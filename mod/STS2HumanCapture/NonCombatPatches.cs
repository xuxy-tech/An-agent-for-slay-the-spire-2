using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Runs;

namespace STS2HumanCapture;

internal static class CaptureSafety
{
    public static void Run(string hook, Action capture)
    {
        try { capture(); }
        catch (Exception ex)
        {
            Log.Warn($"[STS2HumanCapture] {hook} capture failed: {ex.Message}", 2);
        }
    }
}

internal static class CaptureDetail
{
    public static Dictionary<string, object?> Of(params (string key, object? value)[] values) =>
        values.ToDictionary(value => value.key, value => value.value, StringComparer.Ordinal);

    public static object? Coord(MapCoord? coord) => coord is { } value
        ? new { value.col, value.row }
        : null;

    public static string? ModelId(AbstractModel? model) => model?.Id.ToString();

    public static object Models(IEnumerable<AbstractModel>? models) =>
        (models ?? []).Select(model => new
        {
            model_id = ModelId(model),
            model_class = model.GetType().FullName,
            scalar_members = PlayerActionEvents.ScalarMembers(model),
        }).ToList();

    public static Dictionary<string, object?> PlayerChoice(PlayerChoiceResult result)
    {
        var net = result.ToNetData();
        return Of(
            ("choice_type", result.ChoiceType.ToString()),
            ("canonical_cards", Models(net.canonicalCards)),
            ("combat_cards", (net.combatCards ?? []).Select(card => PlayerActionEvents.ScalarMembers(card)).ToList()),
            ("deck_cards", (net.deckCards ?? []).Select(card => PlayerActionEvents.ScalarMembers(card)).ToList()),
            ("mutable_cards", (net.mutableCards ?? []).Select(card => PlayerActionEvents.ScalarMembers(card)).ToList()),
            ("mutable_card_owner", net.mutableCardOwner),
            ("indexes", net.indexes?.ToList() ?? []),
            ("selected_player_id", net.playerId));
    }

    public static Dictionary<string, object?> Reward(Reward reward) => Of(
        ("reward_class", reward.GetType().FullName),
        ("reward_type", PlayerActionEvents.ReadMember(reward, "RewardType")?.ToString()),
        ("reward_set_index", reward.RewardsSetIndex),
        ("reward_player_id", reward.Player.NetId));

    public static Dictionary<string, object?> Merchant(MerchantEntry entry, bool ignoreCost)
    {
        var detail = Of(
            ("entry_class", entry.GetType().FullName),
            ("cost", entry.Cost),
            ("enough_gold", entry.EnoughGold),
            ("is_stocked", entry.IsStocked),
            ("ignore_cost", ignoreCost));
        switch (entry)
        {
            case MerchantCardEntry card:
                detail["model_id"] = ModelId(card.CreationResult?.Card);
                detail["is_on_sale"] = card.IsOnSale;
                break;
            case MerchantRelicEntry relic:
                detail["model_id"] = ModelId(relic.Model);
                break;
            case MerchantPotionEntry potion:
                detail["model_id"] = ModelId(potion.Model);
                break;
            case MerchantCardRemovalEntry removal:
                detail["used"] = removal.Used;
                break;
        }
        return detail;
    }
}

[HarmonyPatch(typeof(PlayerChoiceSynchronizer), nameof(PlayerChoiceSynchronizer.SyncLocalChoice))]
internal static class PlayerChoicePatch
{
    private static void Prefix(Player player, uint choiceId, PlayerChoiceResult result)
    {
        CaptureSafety.Run(nameof(PlayerChoicePatch), () =>
        {
            if (RunManager.Instance.NetService is { } net && player.NetId != net.NetId)
                return;
            var detail = CaptureDetail.PlayerChoice(result);
            detail["choice_id"] = choiceId;
            PlayerActionEvents.PublishObserved("player_choice", "submit_player_choice", detail, "committed");
        });
    }
}

[HarmonyPatch(typeof(MapSelectionSynchronizer), nameof(MapSelectionSynchronizer.PlayerVotedForMapCoord))]
internal static class MapSelectionPatch
{
    private static void Prefix(Player player, MapLocation source, MapVote? destination)
    {
        CaptureSafety.Run(nameof(MapSelectionPatch), () =>
        {
            if (RunManager.Instance.NetService is { } net && player.NetId != net.NetId)
                return;
            PlayerActionEvents.PublishObserved(
                "map_selection",
                destination.HasValue ? "choose_map_node" : "cancel_map_vote",
                CaptureDetail.Of(
                    ("source_act_index", source.actIndex),
                    ("source_coord", CaptureDetail.Coord(source.coord)),
                    ("map_generation_count", destination?.mapGenerationCount),
                    ("destination_coord", destination.HasValue
                        ? CaptureDetail.Coord(destination.Value.coord)
                        : null)));
        });
    }
}

[HarmonyPatch(typeof(EventSynchronizer), nameof(EventSynchronizer.ChooseLocalOption))]
internal static class EventSelectionPatch
{
    private static void Prefix(EventSynchronizer __instance, int index) =>
        CaptureSafety.Run(nameof(EventSelectionPatch), () =>
            PlayerActionEvents.PublishObserved("event_selection", "choose_event_option", CaptureDetail.Of(
                ("option_index", index),
                ("event_model_id", CaptureDetail.ModelId(__instance.GetLocalEvent())),
                ("is_shared", __instance.IsShared))));
}

[HarmonyPatch(typeof(RestSiteSynchronizer), nameof(RestSiteSynchronizer.ChooseLocalOption))]
internal static class RestSiteSelectionPatch
{
    private static void Prefix(RestSiteSynchronizer __instance, int index)
    {
        CaptureSafety.Run(nameof(RestSiteSelectionPatch), () =>
        {
            var options = __instance.GetLocalOptions();
            var option = index >= 0 && index < options.Count ? options[index] : null;
            PlayerActionEvents.PublishObserved("rest_site_selection", "choose_rest_site_option", CaptureDetail.Of(
                ("option_index", index),
                ("option_id", option?.OptionId),
                ("option_class", option?.GetType().FullName)));
        });
    }
}

[HarmonyPatch(typeof(TreasureRoomRelicSynchronizer), nameof(TreasureRoomRelicSynchronizer.PickRelicLocally))]
internal static class TreasureRelicSelectionPatch
{
    private static void Prefix(TreasureRoomRelicSynchronizer __instance, int? index)
    {
        CaptureSafety.Run(nameof(TreasureRelicSelectionPatch), () =>
        {
            var relics = __instance.CurrentRelics ?? Array.Empty<RelicModel>();
            var relic = index is { } value && value >= 0 && value < relics.Count
                ? relics[value]
                : null;
            PlayerActionEvents.PublishObserved("treasure_selection", "choose_treasure_relic", CaptureDetail.Of(
                ("relic_index", index),
                ("model_id", CaptureDetail.ModelId(relic))));
        });
    }
}

[HarmonyPatch(typeof(TreasureRoomRelicSynchronizer), nameof(TreasureRoomRelicSynchronizer.SkipRelicLocally))]
internal static class TreasureRelicSkipPatch
{
    private static void Prefix() => CaptureSafety.Run(nameof(TreasureRelicSkipPatch), () =>
        PlayerActionEvents.PublishObserved("treasure_selection", "skip_treasure_relic"));
}

[HarmonyPatch(typeof(RewardsSetSynchronizer), nameof(RewardsSetSynchronizer.SelectLocalReward))]
internal static class RewardSetSelectionPatch
{
    private static void Prefix(Reward reward) => CaptureSafety.Run(nameof(RewardSetSelectionPatch), () =>
    {
        NativeRewards.Claiming(reward);
        PlayerActionEvents.PublishObserved("reward_selection", "select_reward", CaptureDetail.Reward(reward));
    });
}

[HarmonyPatch(typeof(RewardsSetSynchronizer), nameof(RewardsSetSynchronizer.SkipLocalRewardsSet))]
internal static class RewardSetSkipPatch
{
    private static void Prefix() => CaptureSafety.Run(nameof(RewardSetSkipPatch), () =>
        PlayerActionEvents.PublishObserved("reward_selection", "skip_rewards_set"));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalObtainedCard))]
internal static class ObtainCardRewardPatch
{
    private static void Prefix(CardModel card) => CaptureSafety.Run(nameof(ObtainCardRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "obtain_card", CaptureDetail.Of(
            ("model_id", CaptureDetail.ModelId(card)), ("model", PlayerActionEvents.ScalarMembers(card)))));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalSkippedCard))]
internal static class SkipCardRewardPatch
{
    private static void Prefix(CardModel card) => CaptureSafety.Run(nameof(SkipCardRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "skip_card", CaptureDetail.Of(
            ("model_id", CaptureDetail.ModelId(card)), ("model", PlayerActionEvents.ScalarMembers(card)))));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalObtainedRelic))]
internal static class ObtainRelicRewardPatch
{
    private static void Prefix(RelicModel relic) => CaptureSafety.Run(nameof(ObtainRelicRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "obtain_relic", CaptureDetail.Of(
            ("model_id", CaptureDetail.ModelId(relic)), ("model", PlayerActionEvents.ScalarMembers(relic)))));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalSkippedRelic))]
internal static class SkipRelicRewardPatch
{
    private static void Prefix(RelicModel relic) => CaptureSafety.Run(nameof(SkipRelicRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "skip_relic", CaptureDetail.Of(
            ("model_id", CaptureDetail.ModelId(relic)), ("model", PlayerActionEvents.ScalarMembers(relic)))));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalObtainedPotion))]
internal static class ObtainPotionRewardPatch
{
    private static void Prefix(PotionModel potion) => CaptureSafety.Run(nameof(ObtainPotionRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "obtain_potion", CaptureDetail.Of(
            ("model_id", CaptureDetail.ModelId(potion)), ("model", PlayerActionEvents.ScalarMembers(potion)))));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalSkippedPotion))]
internal static class SkipPotionRewardPatch
{
    private static void Prefix(PotionModel potion) => CaptureSafety.Run(nameof(SkipPotionRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "skip_potion", CaptureDetail.Of(
            ("model_id", CaptureDetail.ModelId(potion)), ("model", PlayerActionEvents.ScalarMembers(potion)))));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalObtainedGold))]
internal static class ObtainGoldRewardPatch
{
    private static void Prefix(int goldAmount) => CaptureSafety.Run(nameof(ObtainGoldRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "obtain_gold", CaptureDetail.Of(
            ("gold_amount", goldAmount))));
}

[HarmonyPatch(typeof(RewardSynchronizer), nameof(RewardSynchronizer.SyncLocalGoldLost))]
internal static class LoseGoldRewardPatch
{
    private static void Prefix(int goldLost) => CaptureSafety.Run(nameof(LoseGoldRewardPatch), () =>
        PlayerActionEvents.PublishObserved("reward_item", "lose_gold", CaptureDetail.Of(
            ("gold_amount", goldLost))));
}

[HarmonyPatch(typeof(MerchantEntry), nameof(MerchantEntry.OnTryPurchaseWrapper),
    [typeof(MerchantInventory), typeof(bool)])]
internal static class MerchantPurchasePatch
{
    private static void Prefix(MerchantEntry __instance, bool ignoreCost) =>
        CaptureSafety.Run(nameof(MerchantPurchasePatch), () =>
            PlayerActionEvents.PublishObserved("merchant_purchase", "purchase_merchant_entry",
                CaptureDetail.Merchant(__instance, ignoreCost)));
}

[HarmonyPatch(typeof(MerchantCardRemovalEntry), nameof(MerchantCardRemovalEntry.OnTryPurchaseWrapper),
    [typeof(MerchantInventory), typeof(bool), typeof(bool)])]
internal static class MerchantCardRemovalPatch
{
    private static void Prefix(MerchantCardRemovalEntry __instance, bool ignoreCost, bool cancelable)
    {
        CaptureSafety.Run(nameof(MerchantCardRemovalPatch), () =>
        {
            var detail = CaptureDetail.Merchant(__instance, ignoreCost);
            detail["cancelable"] = cancelable;
            PlayerActionEvents.PublishObserved("merchant_purchase", "purchase_card_removal", detail);
        });
    }
}
