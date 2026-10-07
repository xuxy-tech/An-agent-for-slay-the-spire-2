using System.Reflection;
using System.Runtime.Loader;
using HarmonyLib;

if (args.Length != 2)
{
    Console.Error.WriteLine("Usage: HumanCapturePatchSmoke <game-assembly-dir> <capture-mod-dll>");
    return 2;
}

var gameAssemblyDir = Path.GetFullPath(args[0]);
var capturePath = Path.GetFullPath(args[1]);
AssemblyLoadContext.Default.Resolving += (_, name) =>
{
    var path = Path.Combine(gameAssemblyDir, name.Name + ".dll");
    return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
};

if (!File.Exists(Path.Combine(gameAssemblyDir, "sts2.dll")))
    throw new FileNotFoundException("sts2.dll was not found", gameAssemblyDir);
if (!File.Exists(capturePath))
    throw new FileNotFoundException("Capture Mod DLL was not found", capturePath);

var captureAssembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(capturePath);
const string owner = "vesper.sts2.human-capture.patch-smoke";
var harmony = new Harmony(owner);
harmony.PatchAll(captureAssembly);
var patched = Harmony.GetAllPatchedMethods()
    .Where(method => Harmony.GetPatchInfo(method)?.Owners.Contains(owner) == true)
    .OrderBy(method => method.DeclaringType?.FullName, StringComparer.Ordinal)
    .ThenBy(method => method.Name, StringComparer.Ordinal)
    .ToList();
var expected = new HashSet<string>(StringComparer.Ordinal)
{
    "MegaCrit.Sts2.Core.Entities.Merchant.MerchantCardRemovalEntry.OnTryPurchaseWrapper",
    "MegaCrit.Sts2.Core.Entities.Merchant.MerchantEntry.OnTryPurchaseWrapper",
    "MegaCrit.Sts2.Core.GameActions.Multiplayer.PlayerChoiceSynchronizer.SyncLocalChoice",
    "MegaCrit.Sts2.Core.Multiplayer.Game.EventSynchronizer.ChooseLocalOption",
    "MegaCrit.Sts2.Core.Multiplayer.Game.MapSelectionSynchronizer.PlayerVotedForMapCoord",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RestSiteSynchronizer.ChooseLocalOption",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalGoldLost",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalObtainedCard",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalObtainedGold",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalObtainedPotion",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalObtainedRelic",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalSkippedCard",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalSkippedPotion",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardSynchronizer.SyncLocalSkippedRelic",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardsSetSynchronizer.SelectLocalReward",
    "MegaCrit.Sts2.Core.Multiplayer.Game.RewardsSetSynchronizer.SkipLocalRewardsSet",
    "MegaCrit.Sts2.Core.Multiplayer.Game.TreasureRoomRelicSynchronizer.PickRelicLocally",
    "MegaCrit.Sts2.Core.Multiplayer.Game.TreasureRoomRelicSynchronizer.SkipRelicLocally",
    "MegaCrit.Sts2.Core.Nodes.CommonUi.NProceedButton.OnRelease",
    "MegaCrit.Sts2.Core.Nodes.Rooms.NMerchantRoom.OpenInventory",
    "MegaCrit.Sts2.Core.Nodes.Rooms.NTreasureRoom.OnChestButtonReleased",
    "MegaCrit.Sts2.Core.Nodes.Screens.Shops.NMerchantInventory.Close",
    "MegaCrit.Sts2.Core.Models.PotionModel.EnqueueManualUse",
};
var patchedNames = patched
    .Select(method => $"{method.DeclaringType?.FullName}.{method.Name}")
    .ToHashSet(StringComparer.Ordinal);
var missing = expected.Except(patchedNames).OrderBy(name => name, StringComparer.Ordinal).ToList();
if (missing.Count > 0)
    throw new InvalidOperationException("Harmony patches missing: " + string.Join(", ", missing));
Console.WriteLine($"PATCHED_METHODS={patched.Count}");
foreach (var method in patched)
    Console.WriteLine($"{method.DeclaringType?.FullName}.{method.Name}");

var gameAssembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.Combine(gameAssemblyDir, "sts2.dll"));
// Native reward identity is read through these members. Fail installation if
// the current game DLL changes their shape, before the first live reward.
var rewardSync = gameAssembly.GetType("MegaCrit.Sts2.Core.Multiplayer.Game.RewardsSetSynchronizer", true)!;
var rewardState = rewardSync.GetMethod("GetRewardStateForPlayer", BindingFlags.Instance | BindingFlags.NonPublic)
    ?? throw new MissingMethodException(rewardSync.FullName, "GetRewardStateForPlayer");
var rewardStack = rewardState.ReturnType.GetField("rewardsStack", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)
    ?? throw new MissingFieldException(rewardState.ReturnType.FullName, "rewardsStack");
var stackElement = rewardStack.FieldType.GenericTypeArguments.Single();
_ = stackElement.GetField("set", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)
    ?? throw new MissingFieldException(stackElement.FullName, "set");
var rewardScreen = gameAssembly.GetType("MegaCrit.Sts2.Core.Nodes.Screens.NRewardsScreen", true)!;
_ = rewardScreen.GetField("_rewardsSet", BindingFlags.Instance | BindingFlags.NonPublic)
    ?? throw new MissingFieldException(rewardScreen.FullName, "_rewardsSet");
Console.WriteLine("NATIVE_REWARD_IDENTITY_CONTRACT=PASS");
var fix = captureAssembly.GetType("STS2HumanCapture.PotionTargetCompatibility", throwOnError: true)!;
var rule = fix.GetMethod("RequiresNullTarget", BindingFlags.Static | BindingFlags.NonPublic)!;
var prefix = fix.GetMethod("Prefix", BindingFlags.Static | BindingFlags.NonPublic)!;
var targetType = gameAssembly.GetType("MegaCrit.Sts2.Core.Entities.Cards.TargetType", true)!;
var nullKinds = new HashSet<string> { "None", "AllEnemies", "RandomEnemy", "AllAllies", "TargetedNoCreature" };
foreach (var kind in Enum.GetNames(targetType))
{
    var actual = (bool)rule.Invoke(null, new[] { Enum.Parse(targetType, kind) })!;
    if (actual != nullKinds.Contains(kind))
        throw new InvalidOperationException("Potion target rule regressed: " + kind);
}
var creatureType = gameAssembly.GetType("MegaCrit.Sts2.Core.Entities.Creatures.Creature", true)!;
var recipient = System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(creatureType);
var potionBase = gameAssembly.GetType("MegaCrit.Sts2.Core.Models.PotionModel", true)!;
var testedKinds = new HashSet<string>();
foreach (var modelType in gameAssembly.GetTypes().Where(t => !t.IsAbstract && potionBase.IsAssignableFrom(t)))
{
    var model = System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(modelType);
    var kind = potionBase.GetProperty("TargetType")!.GetValue(model)!.ToString()!;
    var acceptsNull = (bool)potionBase.GetMethod("IsValidTarget")!.Invoke(model, new object?[] { null })!;
    if (acceptsNull != nullKinds.Contains(kind))
        throw new InvalidOperationException("Native null-target contract changed: " + modelType.Name);
    object?[] parameters = { model, recipient };
    prefix.Invoke(null, parameters);
    if (nullKinds.Contains(kind) ? parameters[1] != null : !ReferenceEquals(parameters[1], recipient))
        throw new InvalidOperationException("Potion recipient regressed: " + modelType.Name);
    parameters[1] = null;
    prefix.Invoke(null, parameters);
    if (parameters[1] != null)
        throw new InvalidOperationException("Potion prefix invented a target: " + modelType.Name);
    testedKinds.Add(kind);
}
foreach (var required in new[] { "AllEnemies", "AnyEnemy", "AnyPlayer" })
    if (!testedKinds.Contains(required))
        throw new InvalidOperationException("Missing real potion coverage: " + required);
Console.WriteLine("POTION_TARGET_REGRESSION=PASS kinds=" + string.Join(",", testedKinds.Order()));

return 0;
