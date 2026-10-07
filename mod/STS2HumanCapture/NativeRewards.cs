using System.Collections;
using System.Reflection;
using Godot;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.ScreenContext;

namespace STS2HumanCapture;

/// <summary>Read native set/item identities without changing rewards or RNG.</summary>
internal static class NativeRewards
{
    public const string Contract = "native-reward-items-v1";
    private static RewardsSet? _claimSet;
    private static Reward? _claimItem;

    private static object? Read(object value, string name) => value.GetType()
        .GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)?.GetValue(value);

    public static void Claiming(Reward reward)
    {
        var sync = RunManager.Instance.RewardsSetSynchronizer;
        var method = sync.GetType().GetMethod("GetRewardStateForPlayer", BindingFlags.Instance | BindingFlags.NonPublic)
            ?? throw new InvalidOperationException("Native reward state accessor is unavailable");
        var playerState = method.Invoke(sync, new object[] { reward.Player })!;
        var stack = ((IEnumerable)Read(playerState, "rewardsStack")!).Cast<object>().ToList();
        _claimSet = stack.Count > 0 ? Read(stack[^1], "set") as RewardsSet : null;
        if (_claimSet == null || !_claimSet.Rewards.Contains(reward))
            throw new InvalidOperationException("Claimed reward has no native set identity");
        _claimItem = reward;
    }

    public static Dictionary<string, object?> Identity(Reward reward, int index) => new()
    {
        ["native_index"] = index,
        ["reward_type"] = reward.GetType().Name.Replace("Reward", ""),
        ["successfully_selected"] = reward.SuccessfullySelected,
        ["model_id"] = reward switch { PotionReward potion => potion.Potion.Id.Entry,
            RelicReward relic => relic.Relic.Id.Entry, _ => null },
        ["amount"] = reward is GoldReward gold ? gold.Amount : null,
        ["cards"] = reward is CardReward card ? card.Cards.Select(c => new { id = c.Id.Entry, upgraded = c.IsUpgraded }).ToList() : null,
    };

    private static IEnumerable<Node> Descendants(Node node)
    {
        foreach (var child in node.GetChildren())
        {
            yield return child;
            foreach (var descendant in Descendants(child)) yield return descendant;
        }
    }

    public static Dictionary<string, object?> Capture()
    {
        var screen = ActiveScreenContext.Instance.GetCurrentScreen();
        var choice = screen is NCardRewardSelectionScreen;
        var set = screen is NRewardsScreen overview ? Read(overview, "_rewardsSet") as RewardsSet
            : choice ? _claimSet : null;
        if (set == null) throw new InvalidOperationException("No native reward set at the current screen");
        var buttons = screen is NRewardsScreen node ? Descendants(node).OfType<NRewardButton>()
            .Where(GodotObject.IsInstanceValid).OrderBy(b => b.GlobalPosition.Y).ThenBy(b => b.GlobalPosition.X).ToList() : [];
        var visible = buttons.Select((button, index) =>
        {
            var nativeIndex = set.Rewards.IndexOf(button.Reward);
            if (nativeIndex < 0) throw new InvalidOperationException("Reward button is outside its native set");
            var row = Identity(button.Reward, nativeIndex);
            row["index"] = index;
            return row;
        }).ToList();
        return new()
        {
            ["contract"] = Contract, ["reward_set_id"] = set.Id,
            ["pending_card_choice"] = choice,
            ["reward_index"] = choice && _claimItem != null ? set.Rewards.IndexOf(_claimItem) : null,
            ["rewards"] = visible,
            ["offered_rewards"] = set.Rewards.Select((reward, index) => Identity(reward, index)).ToList(),
        };
    }
}
