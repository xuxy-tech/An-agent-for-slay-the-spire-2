using HarmonyLib;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;

namespace STS2HumanCapture;

[HarmonyPatch(typeof(NProceedButton), "OnRelease")]
internal static class ProceedButtonPatch
{
    private static void Prefix(NProceedButton __instance) =>
        CaptureSafety.Run(nameof(ProceedButtonPatch), () =>
            PlayerActionEvents.PublishObserved(
                "ui_navigation",
                __instance.IsSkip ? "skip" : "proceed",
                CaptureDetail.Of(
                    ("action_role", "navigation"),
                    ("button_class", __instance.GetType().FullName),
                    ("is_skip", __instance.IsSkip))));
}

[HarmonyPatch(typeof(NTreasureRoom), "OnChestButtonReleased")]
internal static class OpenChestPatch
{
    private static void Prefix() => CaptureSafety.Run(nameof(OpenChestPatch), () =>
        PlayerActionEvents.PublishObserved(
            "ui_navigation", "open_chest",
            CaptureDetail.Of(("action_role", "navigation"))));
}

[HarmonyPatch(typeof(NMerchantRoom), nameof(NMerchantRoom.OpenInventory))]
internal static class OpenShopPatch
{
    private static void Prefix() => CaptureSafety.Run(nameof(OpenShopPatch), () =>
        PlayerActionEvents.PublishObserved(
            "shop_navigation", "open_shop_inventory",
            CaptureDetail.Of(("action_role", "navigation"))));
}

[HarmonyPatch(typeof(NMerchantInventory), "Close")]
internal static class CloseShopPatch
{
    private static void Prefix() => CaptureSafety.Run(nameof(CloseShopPatch), () =>
        PlayerActionEvents.PublishObserved(
            "shop_navigation", "close_shop_inventory",
            CaptureDetail.Of(("action_role", "navigation"))));
}
