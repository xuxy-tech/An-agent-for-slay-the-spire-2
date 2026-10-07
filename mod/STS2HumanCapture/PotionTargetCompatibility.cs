using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Models;

namespace STS2HumanCapture;

// The control Mod supplies Owner.Creature for AllEnemies. The native action
// cancels that invalid target before consuming the potion. Correct only the
// no-creature contract at enqueue, before IDs are serialized into the action.
[HarmonyPatch(typeof(PotionModel), nameof(PotionModel.EnqueueManualUse))]
internal static class PotionTargetCompatibility
{
    internal const string Contract = "native-no-creature-v1";

    internal static bool RequiresNullTarget(TargetType type) => type is
        TargetType.None or TargetType.AllEnemies or TargetType.RandomEnemy or
        TargetType.AllAllies or TargetType.TargetedNoCreature;

    [HarmonyPrefix]
    [HarmonyPriority(Priority.First)]
    private static void Prefix(PotionModel __instance, ref Creature? target)
    {
        if (RequiresNullTarget(__instance.TargetType))
            target = null;
    }
}
