using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using System.Diagnostics;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Characters;
using MegaCrit.Sts2.Core.Multiplayer;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Entities.TreasureRelicPicking;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Runs.History;
using MegaCrit.Sts2.Core.TestSupport;
using HarmonyLib;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Multiplayer.Serialization;
using MegaCrit.Sts2.Core.Entities.Multiplayer;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Saves.Runs;
using MegaCrit.Sts2.Core.Unlocks;
using MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent;
using MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere;

namespace Sts2Headless;

/// <summary>
/// Synchronization context that executes continuations inline immediately.
/// Task.Yield() posts to SynchronizationContext.Current — by executing inline,
/// the yield becomes a no-op and the entire async chain runs synchronously.
/// Uses a recursion guard to queue nested posts and drain them after.
/// </summary>
internal class InlineSynchronizationContext : SynchronizationContext
{
    private readonly Queue<(SendOrPostCallback, object?)> _queue = new();
    private bool _executing;

    public override void Post(SendOrPostCallback d, object? state)
    {
        if (_executing)
        {
            _queue.Enqueue((d, state));
            return;
        }
        // removed debug log

        // Execute inline immediately, then drain any nested posts
        _executing = true;
        try
        {
            d(state);
            // Drain any callbacks that were queued during execution
            while (_queue.Count > 0)
            {
                var (cb, st) = _queue.Dequeue();
                cb(st);
            }
        }
        finally
        {
            _executing = false;
        }
    }

    public override void Send(SendOrPostCallback d, object? state)
    {
        d(state);
    }

    public void Pump()
    {
        // Drain any remaining queued callbacks
        while (_queue.Count > 0)
        {
            var (cb, st) = _queue.Dequeue();
            _executing = true;
            try { cb(st); }
            finally { _executing = false; }
        }
    }
}

/// <summary>
/// Bilingual localization lookup — loads eng/zhs JSON files for display names.
/// </summary>
internal class LocLookup
{
    private readonly Dictionary<string, Dictionary<string, string>> _eng = new();
    private readonly Dictionary<string, Dictionary<string, string>> _zhs = new();

    public LocLookup()
    {
        var baseDir = Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..");
        Load(Path.Combine(baseDir, "localization_eng"), _eng);
        Load(Path.Combine(baseDir, "localization_zhs"), _zhs);
    }

    private static void Load(string dir, Dictionary<string, Dictionary<string, string>> target)
    {
        if (!Directory.Exists(dir)) return;
        foreach (var file in Directory.GetFiles(dir, "*.json"))
        {
            try
            {
                var name = Path.GetFileNameWithoutExtension(file);
                var data = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(file));
                if (data != null) target[name] = data;
            }
            catch { }
        }
    }

    /// <summary>Get bilingual name: "English / 中文" or just the key if not found.</summary>
    public string Name(string table, string key)
    {
        var en = _eng.GetValueOrDefault(table)?.GetValueOrDefault(key);
        var zh = _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);
        if (en != null && zh != null && en != zh) return $"{en} / {zh}";
        return en ?? zh ?? key;
    }

    public string? En(string table, string key) => _eng.GetValueOrDefault(table)?.GetValueOrDefault(key);
    public string? Zh(string table, string key) => _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);

    /// <summary>Strip BBCode tags like [gold], [/blue], [b], [sine], etc.</summary>
    private static string StripBBCode(string text)
    {
        return System.Text.RegularExpressions.Regex.Replace(text, @"\[/?[a-zA-Z_][a-zA-Z0-9_=]*\]", "");
    }

    /// <summary>Language for JSON output: "en" or "zh". Default: "en".</summary>
    public string Lang { get; set; } = "en";

    /// <summary>Return localized string for JSON output based on Lang setting.</summary>
    public string Bilingual(string table, string key)
    {
        if (Lang == "zh")
        {
            var zh = _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);
            if (zh != null) return StripBBCode(zh);
        }
        var en = _eng.GetValueOrDefault(table)?.GetValueOrDefault(key) ?? key;
        return StripBBCode(en);
    }

    // Convenience helpers using ModelId
    public string Card(string entry) => Bilingual("cards", entry + ".title");
    public string Monster(string entry)
    {
        var key = entry + ".name";
        var result = Bilingual("monsters", key);
        // If no dedicated entry, fall back to the base segment key (e.g. DECIMILLIPEDE_SEGMENT_FRONT → DECIMILLIPEDE_SEGMENT)
        if (result == key)
        {
            var lastUnderscore = entry.LastIndexOf('_');
            if (lastUnderscore > 0)
            {
                var baseEntry = entry[..lastUnderscore];
                var baseKey = baseEntry + ".name";
                var baseResult = Bilingual("monsters", baseKey);
                if (baseResult != baseKey) return baseResult;
            }
        }
        return result;
    }
    public string Relic(string entry) => Bilingual("relics", entry + ".title");
    public string Potion(string entry) => Bilingual("potions", entry + ".title");
    public string Power(string entry) => Bilingual("powers", entry + ".title");
    public string Event(string entry) => Bilingual("events", entry + ".title");
    public string Act(string entry) => Bilingual("acts", entry + ".title");

    /// <summary>Resolve a full loc key like "TABLE.KEY.SUB" by searching all tables.</summary>
    public string BilingualFromKey(string locKey)
    {
        if (Lang == "zh")
        {
            foreach (var tableName in _zhs.Keys)
            {
                var zh = _zhs.GetValueOrDefault(tableName)?.GetValueOrDefault(locKey);
                if (zh != null) return zh;
            }
        }
        foreach (var tableName in _eng.Keys)
        {
            var en = _eng.GetValueOrDefault(tableName)?.GetValueOrDefault(locKey);
            if (en != null) return en;
        }
        return locKey;
    }

    public bool IsLoaded => _eng.Count > 0;
}

/// <summary>
/// Full run simulator — manages the game lifecycle from character selection
/// through map navigation, combat, events, rest sites, shops, and act transitions.
/// Drives the engine forward until it hits a "decision point" requiring external input.
/// </summary>
public class RunSimulator
{
    public sealed class CombatChildExpansionRequest
    {
        public required string Action { get; init; }
        public Dictionary<string, object?>? Args { get; init; }
        public required string SnapshotId { get; init; }
    }

    private static readonly System.Text.Json.JsonSerializerOptions SnapshotJsonOpts = new()
    {
        IncludeFields = true,
        WriteIndented = false,
    };

    private sealed class CombatSnapshot
    {
        public sealed class SerializedEnvelope
        {
            public required string Id { get; init; }
            public required string CharacterName { get; init; }
            public required int AscensionLevel { get; init; }
            public required string Seed { get; init; }
            [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
            public int? ActIndex { get; init; }
            [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
            public int? ActFloor { get; init; }
            [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
            public string? BossEncounterId { get; init; }
            public required string RoomJson { get; init; }
            public required string PlayerJson { get; init; }
            public required PlainNetState NetState { get; init; }
            public required List<EnemyCreatureSnapshot> EnemyCreatureStates { get; init; }
            public required List<EnemyAiSnapshot> EnemyAiStates { get; init; }
            public required List<RngStateSnapshot> RunRngStates { get; init; }
            public required List<RngStateSnapshot> PlayerRngStates { get; init; }
            public required int RoundNumber { get; init; }
            public required CombatSide CurrentSide { get; init; }
            public List<RelicStateSnapshot>? RelicStates { get; init; }
            public List<PrimitiveObjectStateSnapshot>? HookStates { get; init; }
            public PrimitiveObjectStateSnapshot? PlayerCombatState { get; init; }
            public PrimitiveObjectStateSnapshot? PlayerExtraState { get; init; }
            public List<RuntimeValueSnapshot>? CombatHistoryEntries { get; init; }
            public List<PowerRuntimeRefsSnapshot>? ActivePowerRefs { get; init; }
        }

        public sealed class PlainModelId
        {
            public string? Category;
            public string? Entry;
        }

        public sealed class PlainEnergyCost
        {
            public int? Value;
            public int? ResolvedValue;
        }

        public sealed class PlainLocalCostModifier
        {
            public int Amount;
            public int Type;
            public int Expiration;
            public bool IsReduceOnly;
        }

        public sealed class PlainRuntimeEnergyCost
        {
            public int Base;
            public int Canonical;
            public bool CostsX;
            public int CapturedXValue;
            public bool WasJustUpgraded;
            public List<PlainLocalCostModifier> LocalModifiers = new();
        }

        public sealed class PlainSerializableEnchantment
        {
            public required PlainModelId id;
            public int amount;
            public SavedProperties? props;
        }

        public sealed class PlainSerializableCard
        {
            public required PlainModelId id;
            public int floor_added_to_deck;
            public int? CurrentUpgradeLevel;
            public PlainSerializableEnchantment? enchantment;
        }

        public sealed class PlainCardState
        {
            public required PlainSerializableCard card;
            public PlainModelId? affliction;
            public int afflictionCount;
            public PlainEnergyCost? energyCost;
            public PlainRuntimeEnergyCost? runtimeEnergyCost;
            public List<string>? keywords;
        }

        public sealed class PlainPileState
        {
            public int pileType;
            public required List<PlainCardState> cards;
        }

        public sealed class PlainCountersContainer
        {
            public required Dictionary<string, int> Counters;
        }

        public sealed class PlainPowerState
        {
            public required PlainModelId id;
            public int amount;
        }

        public sealed class PlainPlayerState
        {
            public ulong? playerId;
            public PlainModelId? characterId;
            public int energy;
            public int stars;
            public int maxStars;
            public int maxPotionCount;
            public int gold;
            public required List<PlainPileState> piles;
            public PlainCountersContainer? rngSet;
        }

        public sealed class PlainCreatureState
        {
            public PlainModelId? monsterId;
            public ulong? playerId;
            public int currentHp;
            public int maxHp;
            public int block;
            public required List<PlainPowerState> powers;
        }

        public sealed class PlainNetRngState
        {
            public string? Seed;
            public required Dictionary<string, int> Counters;
        }

        public sealed class PlainNetState
        {
            public required List<PlainCreatureState> Creatures;
            public required List<PlainPlayerState> Players;
            public required PlainNetRngState Rng;
        }

        public sealed class RngStateSnapshot
        {
            public required string Name { get; init; }
            public int Counter { get; init; }
            public uint? Seed { get; init; }
            // STS2 uses MegaRandom (xoshiro-like state), not System.Random's
            // CompatPrng. Keep the old fields for backwards-compatible imports,
            // but persist the actual engine state as well.
            public ulong? S0 { get; init; }
            public ulong? S1 { get; init; }
            public ulong? S2 { get; init; }
            public ulong? S3 { get; init; }
            public int? Inext { get; init; }
            public int? Inextp { get; init; }
            public List<int>? SeedArray { get; init; }
        }

        public sealed class EnemyPowerSnapshot
        {
            public required string Id { get; init; }
            public required int Amount { get; init; }
            public int? AmountOnTurnStart { get; init; }
        }

        public sealed class EnemyCreatureSnapshot
        {
            public required string MonsterId { get; init; }
            public string? SlotName { get; init; }
            public required int CurrentHp { get; init; }
            public required int MaxHp { get; init; }
            public required int Block { get; init; }
            public int? MonsterMaxHpBeforeModification { get; init; }
            public uint? CombatId { get; init; }
            public bool? SpawnedThisTurn { get; init; }
            public RngStateSnapshot? MonsterRng { get; init; }
            public required List<EnemyPowerSnapshot> Powers { get; init; }
        }

        public sealed class EnemyAiSnapshot
        {
            public sealed class MoveStateSnapshot
            {
                public required string StateId { get; init; }
                // Preserve the engine's two distinct successor representations.
                // FollowUpStateId is a deferred lookup; FollowUpState is an
                // already-resolved object and changes transition behavior.
                public string? FollowUpStateId { get; init; }
                public string? ResolvedFollowUpStateId { get; init; }
                public bool MustPerformOnceBeforeTransitioning { get; init; }
                public bool PerformedAtLeastOnce { get; init; }
                public required List<string> IntentTypeNames { get; init; }
                public string? PerformOwnerPowerId { get; init; }
                public string? PerformMethodName { get; init; }
                public string? PerformDeclaringType { get; init; }

                // Same-process hot restore can reuse the immutable callback and
                // intent definitions, but never the mutable MoveState instance.
                // PerformMove deliberately replaces its callback with UnsetMove,
                // so retaining the MoveState object itself poisons sibling search
                // branches after the first enemy turn executes it.
                [System.Text.Json.Serialization.JsonIgnore]
                public object? OnPerformRef { get; set; }
                [System.Text.Json.Serialization.JsonIgnore]
                public List<object>? IntentRefs { get; set; }
            }

            public required string MonsterId { get; init; }
            public string? CurrentStateId { get; init; }
            public string? InitialStateId { get; init; }
            public string? NextMoveId { get; init; }
            public bool? PerformedFirstMove { get; init; }
            public required List<string> StateLogIds { get; init; }
            public Dictionary<string, MoveStateSnapshot>? MoveStates { get; init; }
        }

        public required string Id { get; init; }
        public required string CharacterName { get; init; }
        public required int AscensionLevel { get; init; }
        public required string Seed { get; init; }
        public int? ActIndex { get; init; }
        public int? ActFloor { get; init; }
        public string? BossEncounterId { get; init; }
        public required SerializableRoom Room { get; init; }
        public required SerializablePlayer Player { get; init; }
        public required object NetState { get; init; }
        public Dictionary<int, List<PlainRuntimeEnergyCost>>? RuntimeCardCosts { get; init; }
        public required List<EnemyCreatureSnapshot> EnemyCreatureStates { get; init; }
        public required List<EnemyAiSnapshot> EnemyAiStates { get; init; }
        public required List<RngStateSnapshot> RunRngStates { get; init; }
        public required List<RngStateSnapshot> PlayerRngStates { get; init; }
        public required int RoundNumber { get; init; }
        public required CombatSide CurrentSide { get; init; }

        // Per-combat relic instance state (e.g. Vambrace's _blockGainedThisCombat).
        // Relics carry combat-scoped flags that are NOT part of the serialized
        // run/combat state, so a full restore rebuilds them fresh but an in_place
        // restore would otherwise leave them at whatever the live combat last
        // mutated them to. Without this, a search that plays a block card
        // (tripping Vambrace's once-per-combat double) and then restores the root
        // to explore another line would see the second line's block cards NOT
        // doubled — a silent state leak that diverges search scores from a cold
        // worker. Keyed by relic id + field name with primitive values, so it
        // round-trips through the cross-process export/import JSON envelope too
        // (an imported root must reset relic flags just like an in-process one).
        public List<RelicStateSnapshot>? RelicStates { get; init; }
        public List<PrimitiveObjectStateSnapshot>? HookStates { get; init; }
        public PrimitiveObjectStateSnapshot? PlayerCombatState { get; init; }
        public PrimitiveObjectStateSnapshot? PlayerExtraState { get; init; }
        public List<RuntimeValueSnapshot>? CombatHistoryEntries { get; init; }
        public List<PowerRuntimeRefsSnapshot>? ActivePowerRefs { get; init; }
        [System.Text.Json.Serialization.JsonIgnore]
        public List<object>? CombatHistoryEntryRefs { get; init; }
        [System.Text.Json.Serialization.JsonIgnore]
        public string? CachedStateFingerprint { get; set; }
        [System.Text.Json.Serialization.JsonIgnore]
        public string? CachedSemanticStateFingerprint { get; set; }
    }

    // Captured per-combat instance state of one relic: primitive field values
    // keyed by name, plus the relic id used to find the live relic on restore.
    // See CombatSnapshot.RelicStates for the rationale. Only JSON-serializable
    // primitive/enum/string fields are captured; complex refs (e.g. a triggering
    // CardModel) are skipped — resetting the primitive flag is what fixes the
    // leak, and the ref is repopulated by the engine on the next trigger.
    public sealed class RelicStateSnapshot
    {
        public required string Id { get; init; }
        public required Dictionary<string, object?> Fields { get; init; }
        // Names of reference-type instance fields that were NULL at capture time.
        // We cannot serialize a live object ref (e.g. Vambrace._triggeringCard, a
        // CardModel) across processes, but combat-scoped refs are null at combat
        // start, and the doubling logic gates on _triggeringCard being null. So a
        // stale non-null ref left by a prior search line would suppress the bonus
        // even after the bool flag is reset. Recording which refs were null lets
        // the restore null them back, which is the state that matters here.
        public List<string>? NullRefFields { get; init; }
    }

    public sealed class PrimitiveFieldSnapshot
    {
        public required string DeclaringType { get; init; }
        public required string Name { get; init; }
        public required string FieldType { get; init; }
        public object? Value { get; init; }
    }

    public sealed class PrimitiveObjectStateSnapshot
    {
        public required string TypeName { get; init; }
        public int TypeOrdinal { get; init; }
        public required List<PrimitiveFieldSnapshot> Fields { get; init; }
    }

    public sealed class RuntimeFieldSnapshot
    {
        public required string DeclaringType { get; init; }
        public required string Name { get; init; }
        public required RuntimeValueSnapshot Value { get; init; }
    }

    public sealed class RuntimeMapEntrySnapshot
    {
        public required RuntimeValueSnapshot Key { get; init; }
        public required RuntimeValueSnapshot Value { get; init; }
    }

    public sealed class RuntimeValueSnapshot
    {
        public required string Kind { get; init; }
        public string? TypeName { get; init; }
        public string? ScalarJson { get; init; }
        public string? NativeJson { get; init; }
        public bool IsPlayerCreature { get; init; }
        public int? CreatureIndex { get; init; }
        public int? PileType { get; init; }
        public int? CardIndex { get; init; }
        public int? AllCardIndex { get; init; }
        public int? PowerIndex { get; init; }
        public string? ModelId { get; init; }
        public string? SlotName { get; init; }
        public string? MovePath { get; init; }
        public string? OwnerModelId { get; init; }
        public PrimitiveObjectStateSnapshot? MovePrimitiveState { get; init; }
        public string? HistoricalEnemyAiJson { get; init; }
        [System.Text.Json.Serialization.JsonIgnore(Condition =
            System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
        public string? HistoricalCreatureStateJson { get; init; }
        public string? HistoricalMonsterRngJson { get; init; }
        public bool? HistoricalHadMoveStateMachine { get; init; }
        public List<RuntimeFieldSnapshot>? Fields { get; init; }
        public List<RuntimeValueSnapshot>? Items { get; init; }
        public List<RuntimeMapEntrySnapshot>? Entries { get; init; }
        public int? ObjectId { get; init; }
        public int? RefId { get; init; }
    }

    public sealed class PowerRuntimeRefsSnapshot
    {
        public required int CreatureIndex { get; init; }
        public required int PowerIndex { get; init; }
        public required string PowerId { get; init; }
        public required RuntimeValueSnapshot Applier { get; init; }
        public required RuntimeValueSnapshot Target { get; init; }
        public RuntimeValueSnapshot? InternalData { get; init; }
        public List<PowerDynamicVarSnapshot>? DynamicVars { get; init; }
    }

    public sealed class PowerDynamicVarSnapshot
    {
        public required string Name { get; init; }
        public required PrimitiveObjectStateSnapshot State { get; init; }
    }

    private static int? _expectedSaveSchemaVersion;
    private static bool _expectedSaveSchemaVersionReady;
    private static readonly object _expectedSaveSchemaVersionLock = new();

    private RunState? _runState;
    private static bool _modelDbInitialized;
    private static volatile CrystalSphereMinigame? _activeCrystalSphere;
    private static readonly InlineSynchronizationContext _syncCtx = new();
    private readonly ManualResetEventSlim _turnStarted = new(false);
    private readonly ManualResetEventSlim _combatEnded = new(false);
    private static readonly LocLookup _loc = new();
    private Task? _pendingInteractionTask;
    private RewardsSet? _eventRewardsSet;
    private TaskCompletionSource? _eventRewardsSelection;
    private Task<bool>? _pendingRewardClaimTask;
    private int? _activeRewardIndex;

    // Keep the native room-end set alive across the player's card choice.
    private RewardsSet? _combatRewardsSet;
    private Task? _combatRewardsCompletion;
    private readonly List<(int Index, Reward Reward)> _pendingCombatRewards = new();
    private CardReward? _activeCombatCardReward;
    private bool _rewardsProcessed;
    private int _goldBeforeCombat;
    private int _lastKnownHp;
    private readonly HeadlessCardSelector _cardSelector = new();
    // Pending bundle selection (Scroll Boxes: pick 1 of N packs)
    private IReadOnlyList<IReadOnlyList<CardModel>>? _pendingBundles;
    private TaskCompletionSource<IEnumerable<CardModel>>? _pendingBundleTcs;
    private bool _combatHandlersRegistered;
    private bool _cardSelectorInstalled;
    private readonly Dictionary<string, CombatSnapshot> _combatSnapshots = new();
    private int _autoResolvedRelicPicks;
    private Dictionary<string, object?>? _lastAutoResolvedRelicPick;
    private bool _treasureChestOpened;
    private bool _treasureRelicClaimed;

    // Search actions are synchronous from the caller's perspective, but one
    // native action may drain many queued continuations. Keep this telemetry
    // separate from game state so profiling cannot affect combat semantics.
    private bool _actionExecutionProfileActive;
    private int _actionWaitCalls;
    private int _actionWaitIterations;
    private int _actionWaitMaxIterations;
    private int _actionWaitSleepCalls;
    private double _actionWaitTotalMs;
    private double _actionWaitSleepMs;
    private int _actionEndTurnPumpIterations;
    private int _actionEndTurnStalls;
    private double _actionEndTurnPumpSleepMs;

    public void BeginActionExecutionProfile()
    {
        _actionExecutionProfileActive = true;
        _actionWaitCalls = 0;
        _actionWaitIterations = 0;
        _actionWaitMaxIterations = 0;
        _actionWaitSleepCalls = 0;
        _actionWaitTotalMs = 0.0;
        _actionWaitSleepMs = 0.0;
        _actionEndTurnPumpIterations = 0;
        _actionEndTurnStalls = 0;
        _actionEndTurnPumpSleepMs = 0.0;
    }

    public void AttachActionExecutionProfile(Dictionary<string, object?> result)
    {
        result["headless_wait_profile"] = new Dictionary<string, object?>
        {
            ["wait_calls"] = _actionWaitCalls,
            ["wait_iterations"] = _actionWaitIterations,
            ["max_wait_iterations"] = _actionWaitMaxIterations,
            ["sleep_calls"] = _actionWaitSleepCalls,
            ["wait_total_ms"] = _actionWaitTotalMs,
            ["sleep_total_ms"] = _actionWaitSleepMs,
            ["end_turn_pump_iterations"] = _actionEndTurnPumpIterations,
            ["end_turn_pump_sleep_ms"] = _actionEndTurnPumpSleepMs,
            ["end_turn_stalls"] = _actionEndTurnStalls,
        };
        _actionExecutionProfileActive = false;
    }

    private void ResetTreasureInteractionState()
    {
        _treasureChestOpened = false;
        _treasureRelicClaimed = false;
    }

    private void OnTurnStartedForHeadless(CombatState _)
    {
        _turnStarted.Set();
    }

    private void OnCombatEndedForHeadless(CombatRoom _)
    {
        _combatEnded.Set();
    }

    private void RegisterCombatEventHandlers()
    {
        if (_combatHandlersRegistered)
            return;
        CombatManager.Instance.TurnStarted += OnTurnStartedForHeadless;
        CombatManager.Instance.CombatEnded += OnCombatEndedForHeadless;
        _combatHandlersRegistered = true;
    }

    private void EnsureCardSelectorInstalled()
    {
        if (_cardSelectorInstalled)
            return;
        CardSelectCmd.UseSelector(_cardSelector);
        // Use the game's supported test selector: Offer still generates its
        // native set, begins synchronization and checks completion. Only the
        // absent UI's choice loop is replaced with an observable pause.
        RewardsSet.testSelector = PauseEventRewards;
        LocPatches._bundleSimRef = this;
        _cardSelectorInstalled = true;
    }

    private Task PauseEventRewards(RewardsSet set)
    {
        if (_eventRewardsSet != null || _pendingCombatRewards.Count != 0)
            throw new InvalidOperationException("Overlapping native reward sets are unsupported");
        _eventRewardsSelection = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        foreach (var (reward, index) in set.Rewards.Select((reward, index) => (reward, index)))
            _pendingCombatRewards.Add((index, reward));
        _eventRewardsSet = set; // publish only after the offer list is complete
        return _eventRewardsSelection.Task;
    }

    private void UnregisterCombatEventHandlers()
    {
        if (!_combatHandlersRegistered)
            return;
        CombatManager.Instance.TurnStarted -= OnTurnStartedForHeadless;
        CombatManager.Instance.CombatEnded -= OnCombatEndedForHeadless;
        _combatHandlersRegistered = false;
    }

    private static void HardResetRunManagerForTest()
    {
        var runManager = RunManager.Instance;
        var runManagerType = runManager.GetType();
        var nonPublic = BindingFlags.Instance | BindingFlags.NonPublic;
        void SetField(string name, object? value)
        {
            runManagerType.GetField(name, nonPublic)?.SetValue(runManager, value);
        }

        SetField("<State>k__BackingField", null);
        SetField("<IsCleaningUp>k__BackingField", false);
        SetField("<IsAbandoned>k__BackingField", false);
        SetField("<ShouldSave>k__BackingField", false);
        SetField("<NetService>k__BackingField", null);
        SetField("<RunLobby>k__BackingField", null);
    }

    public Dictionary<string, object?> StartRun(
        string character,
        int ascension = 0,
        string? seed = null,
        string lang = "en",
        string unlockMode = "all",
        string? progressJson = null)
    {
        try
        {
            var startTimings = new Dictionary<string, object?>();
            var sw = System.Diagnostics.Stopwatch.StartNew();
            long lastMs = 0;
            void Mark(string name)
            {
                var now = sw.ElapsedMilliseconds;
                startTimings[name] = now - lastMs;
                lastMs = now;
            }

            _loc.Lang = lang;
            EnsureModelDbInitialized();
            Mark("init_and_modeldb_ms");

            if (_runState != null || RunManager.Instance.DebugOnlyGetState() != null || RunManager.Instance.IsInProgress)
            {
                CleanUp(keepProcessAlive: true);
                Mark("pre_start_cleanup_ms");
            }

            if (!TryResolveUnlockState(unlockMode, progressJson, out var unlockState,
                    out var unlockSummary, out var unlockError))
                return Error(unlockError);

            var player = CreatePlayer(character, unlockState);
            if (player == null)
                return Error($"Unknown character: {character}");
            Mark("create_player_ms");

            var seedStr = seed ?? "headless_" + DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            Log($"Creating RunState with seed={seedStr}");

            // Use CreateForTest which properly handles mutable copies internally
            _runState = RunState.CreateForTest(
                players: new[] { player },
                ascensionLevel: ascension,
                seed: seedStr
            );
            Mark("create_runstate_ms");

            // Set up RunManager with test mode
            var netService = new NetSingleplayerGameService();
            RunManager.Instance.SetUpTest(_runState, netService);
            LocalContext.NetId = netService.NetId;
            Mark("setup_test_ms");

            // Force Neow event (blessing selection at start)
            _runState.ExtraFields.StartedWithNeow = true;
            Mark("set_neow_flag_ms");

            // Generate rooms for all acts
            RunManager.Instance.GenerateRooms();
            Log("Rooms generated");
            Mark("generate_rooms_ms");

            // Launch the run
            RunManager.Instance.Launch();
            Log("Run launched");
            Mark("launch_ms");

            // Register event handlers for combat turn transitions
            RegisterCombatEventHandlers();
            Mark("register_handlers_ms");

            // Finalize starting relics
            RunManager.Instance.FinalizeStartingRelics().GetAwaiter().GetResult();
            Log("Starting relics finalized");
            Mark("finalize_starting_relics_ms");

            // Enter first act (generates map)
            RunManager.Instance.EnterAct(0, doTransition: false).GetAwaiter().GetResult();
            Log("Entered Act 0");
            Mark("enter_act_ms");

            // Register card selector for cards that need player choice
            EnsureCardSelectorInstalled();
            Mark("register_selectors_ms");

            // Now we should be at the map — detect decision point
            var result = DetectDecisionPoint();
            Mark("detect_decision_point_ms");
            startTimings["total_ms"] = sw.ElapsedMilliseconds;
            result["unlock_profile"] = unlockSummary;
            result["start_run_profile"] = startTimings;
            return result;
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("StartRun failed", ex);
        }
    }

    public Dictionary<string, object?> StartTestCombat(string character, string encounter, int ascension = 0, string? seed = null, string lang = "en")
    {
        try
        {
            var timings = new Dictionary<string, object?>();
            var sw = System.Diagnostics.Stopwatch.StartNew();
            long lastMs = 0;
            void Mark(string name)
            {
                var now = sw.ElapsedMilliseconds;
                timings[name] = now - lastMs;
                lastMs = now;
            }

            _loc.Lang = lang;
            EnsureModelDbInitialized();
            Mark("init_and_modeldb_ms");

            if (_runState != null || RunManager.Instance.DebugOnlyGetState() != null || RunManager.Instance.IsInProgress)
            {
                CleanUp(keepProcessAlive: true);
                Mark("pre_start_cleanup_ms");
            }

            var player = CreatePlayer(character, UnlockState.all);
            if (player == null)
                return Error($"Unknown character: {character}");
            Mark("create_player_ms");

            var seedStr = seed ?? "headless_" + DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            _runState = RunState.CreateForTest(
                players: new[] { player },
                ascensionLevel: ascension,
                seed: seedStr
            );
            Mark("create_runstate_ms");

            var netService = new NetSingleplayerGameService();
            RunManager.Instance.SetUpTest(_runState, netService);
            LocalContext.NetId = netService.NetId;
            Mark("setup_test_ms");

            RegisterCombatEventHandlers();
            EnsureCardSelectorInstalled();
            Mark("register_handlers_and_selectors_ms");

            RunManager.Instance.FinalizeStartingRelics().GetAwaiter().GetResult();
            Mark("finalize_starting_relics_ms");

            var encModel = ModelDb.GetById<EncounterModel>(new ModelId("ENCOUNTER", encounter));
            if (encModel == null)
                return Error($"Unknown encounter: {encounter}");
            var room = new CombatRoom(encModel.ToMutable(), _runState);
            Mark("construct_room_ms");

            RunManager.Instance.EnterRoom(room).GetAwaiter().GetResult();
            _syncCtx.Pump();
            WaitForActionExecutor();
            EnsureSyntheticRoomHistory();
            Mark("enter_room_ms");

            var result = DetectDecisionPoint();
            Mark("detect_decision_point_ms");
            timings["total_ms"] = sw.ElapsedMilliseconds;
            result["start_test_combat_profile"] = timings;
            return result;
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("StartTestCombat failed", ex);
        }
    }

    // ─── Test/Debug commands ───

    private static readonly System.Reflection.BindingFlags NonPublic =
        System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic;

    /// <summary>Get the backing List&lt;T&gt; behind an IReadOnlyList property via reflection.</summary>
    private static List<T>? GetBackingList<T>(object obj, string fieldName)
    {
        var field = obj.GetType().GetField(fieldName, NonPublic);
        return field?.GetValue(obj) as List<T>;
    }

    private void EnsureSyntheticRoomHistory()
    {
        // Debug room entry bypasses map travel. Native card reward selection
        // records its choice in the current map point even in headless mode.
        if (_runState == null || _runState.CurrentMapPointHistoryEntry != null)
            return;
        var history = GetBackingList<List<MapPointHistoryEntry>>(_runState, "_mapPointHistory")
            ?? throw new InvalidOperationException("Test run has no map point history storage");
        history.Add(new List<MapPointHistoryEntry>
        {
            new MapPointHistoryEntry(default(MapPointType), _runState),
        });
    }

    private static void SetField(object obj, string fieldName, object? value)
    {
        for (var type = obj.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetField(fieldName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (field != null)
            {
                field.SetValue(obj, value);
                return;
            }
        }
    }

    private static void SetRequiredField(object obj, string fieldName, object? value)
    {
        for (var type = obj.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetField(fieldName, BindingFlags.Instance | BindingFlags.Public |
                BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (field == null)
                continue;
            field.SetValue(obj, value);
            return;
        }
        throw new InvalidOperationException($"Required restore field {obj.GetType().FullName}.{fieldName} is unavailable");
    }

    private static void SetMaybeField(object obj, string fieldName, object? value)
    {
        for (var type = obj.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetField(fieldName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (field == null)
                continue;
            if (value == null || field.FieldType.IsInstanceOfType(value))
                field.SetValue(obj, value);
            return;
        }
    }

    public Dictionary<string, object?> SetPlayer(Dictionary<string, System.Text.Json.JsonElement> args)
    {
        try
        {
            if (_runState == null) return Error("No run in progress");
            var player = _runState.Players[0];

            if (args.TryGetValue("hp", out var hpEl) && player.Creature != null)
                SetField(player.Creature, "_currentHp", hpEl.GetInt32());
            if (args.TryGetValue("max_hp", out var mhpEl) && player.Creature != null)
                SetField(player.Creature, "_maxHp", mhpEl.GetInt32());
            if (args.TryGetValue("gold", out var goldEl))
                player.Gold = goldEl.GetInt32();

            if (args.TryGetValue("relics", out var relicsEl))
            {
                var list = GetBackingList<RelicModel>(player, "_relics");
                if (list != null)
                {
                    list.Clear();
                    foreach (var rEl in relicsEl.EnumerateArray())
                    {
                        var id = rEl.GetString();
                        if (id == null) continue;
                        var model = ModelDb.GetById<RelicModel>(new ModelId("RELIC", id));
                        if (model != null) list.Add(model.ToMutable());
                    }
                }
            }
            if (args.TryGetValue("deck", out var deckEl))
            {
                // Remove existing cards from RunState tracking
                foreach (var c in player.Deck.Cards.ToList())
                    _runState.RemoveCard(c);
                player.Deck.Clear(silent: true);
                // Add new cards via RunState.CreateCard (sets Owner + registers)
                foreach (var cEl in deckEl.EnumerateArray())
                {
                    var id = cEl.GetString();
                    if (id == null) continue;
                    var canonical = ModelDb.GetById<CardModel>(new ModelId("CARD", id));
                    if (canonical != null)
                    {
                        var card = _runState.CreateCard(canonical, player);
                        player.Deck.AddInternal(card, silent: true);
                    }
                }
            }
            if (args.TryGetValue("potions", out var potionsEl))
            {
                var slots = GetBackingList<PotionModel>(player, "_potionSlots")
                         ?? GetBackingList<PotionModel?>(player, "_potionSlots") as System.Collections.IList;
                if (slots != null)
                {
                    for (int i = 0; i < slots.Count; i++) slots[i] = null;
                    int idx = 0;
                    foreach (var pEl in potionsEl.EnumerateArray())
                    {
                        if (idx >= slots.Count) break;
                        var id = pEl.GetString();
                        if (id != null)
                        {
                            var model = ModelDb.GetById<PotionModel>(new ModelId("POTION", id));
                            if (model != null) slots[idx] = model;
                        }
                        idx++;
                    }
                }
            }

            Log($"SetPlayer: hp={player.Creature?.CurrentHp} gold={player.Gold} relics={player.Relics.Count} deck={player.Deck?.Cards?.Count}");
            return new Dictionary<string, object?>
            {
                ["type"] = "ok",
                ["player"] = PlayerSummary(player),
            };
        }
        catch (Exception ex) { return ErrorWithTrace("SetPlayer failed", ex); }
    }

    public Dictionary<string, object?> EnterRoom(string roomType, string? encounter, string? eventId)
    {
        try
        {
            if (_runState == null) return Error("No run in progress");
            var runState = _runState;
            Log($"EnterRoom: type={roomType} encounter={encounter} event={eventId}");

            ResetTreasureInteractionState();
            AbstractRoom room;
            switch (roomType.ToLowerInvariant())
            {
                case "combat":
                case "monster":
                case "elite":
                {
                    if (string.IsNullOrEmpty(encounter))
                        encounter = "SHRINKER_BEETLE_WEAK"; // default encounter
                    var encModel = ModelDb.GetById<EncounterModel>(new ModelId("ENCOUNTER", encounter));
                    if (encModel == null) return Error($"Unknown encounter: {encounter}");
                    room = new CombatRoom(encModel.ToMutable(), runState);
                    break;
                }
                case "shop":
                    room = new MerchantRoom();
                    break;
                case "rest":
                case "rest_site":
                    room = new RestSiteRoom();
                    break;
                case "event":
                {
                    if (string.IsNullOrEmpty(eventId))
                        return Error("event requires 'event' parameter (e.g. CHANGELING_GROVE)");
                    var evModel = ModelDb.GetById<EventModel>(new ModelId("EVENT", eventId));
                    if (evModel == null) return Error($"Unknown event: {eventId}");
                    room = new EventRoom(evModel);
                    break;
                }
                case "treasure":
                    room = new TreasureRoom(_runState.CurrentActIndex);
                    break;
                default:
                    return Error($"Unknown room type: {roomType}");
            }

            RunManager.Instance.EnterRoom(room).GetAwaiter().GetResult();
            _syncCtx.Pump();
            WaitForActionExecutor();
            EnsureSyntheticRoomHistory();
            return DetectDecisionPoint();
        }
        catch (Exception ex) { return ErrorWithTrace("EnterRoom failed", ex); }
    }

    public Dictionary<string, object?> ConfigureSandbox(System.Text.Json.JsonElement args)
    {
        try
        {
            if (_runState?.CurrentRoom is not CombatRoom room)
                return Error("Sandbox configuration requires an active combat");
            var player = _runState.Players[0];
            var pcs = player.PlayerCombatState;
            if (pcs?.Hand == null || pcs.DrawPile == null || player.Creature == null ||
                DetectDecisionPoint().GetValueOrDefault("decision")?.ToString() != "combat_play")
                return Error("Sandbox configuration requires a player-turn boundary");
            int hp = args.TryGetProperty("hp", out var hpEl) ? hpEl.GetInt32() : player.Creature.CurrentHp;
            int energy = args.TryGetProperty("energy", out var energyEl) ? energyEl.GetInt32() : pcs.Energy;
            if (hp < 1 || hp > player.Creature.MaxHp || energy < 0 || energy > 10)
                return Error("Sandbox HP or energy out of range");
            var enemies = room.CombatState.Enemies.Where(e => e.IsAlive).ToList();
            var enemyHp = args.TryGetProperty("enemy_hp", out var enemyEl)
                ? enemyEl.EnumerateArray().Select(e => e.GetInt32()).ToList() : null;
            if (enemyHp != null && (enemyHp.Count != enemies.Count || enemyHp.Any(v => v < 1 || v > 999)))
                return Error("Sandbox enemy HP count or value invalid");
            var selected = new List<CardModel>();
            if (args.TryGetProperty("hand", out var handEl))
            {
                var available = pcs.Hand.Cards.Concat(pcs.DrawPile.Cards).ToList();
                foreach (var value in handEl.EnumerateArray())
                {
                    var card = available.FirstOrDefault(c => c.Id.Entry == value.GetString());
                    if (card == null) return Error("Requested hand card is absent from deck: " + value.GetString());
                    selected.Add(card);
                    available.Remove(card);
                }
                if (selected.Count < 1 || selected.Count > 10) return Error("Sandbox hand size must be 1-10");
            }
            if (selected.Count > 0)
            {
                foreach (var card in pcs.Hand.Cards.ToList())
                    MegaCrit.Sts2.Core.Commands.CardPileCmd.Add(card, MegaCrit.Sts2.Core.Entities.Cards.PileType.Draw).GetAwaiter().GetResult();
                foreach (var card in selected)
                    MegaCrit.Sts2.Core.Commands.CardPileCmd.Add(card, MegaCrit.Sts2.Core.Entities.Cards.PileType.Hand).GetAwaiter().GetResult();
            }
            if (args.TryGetProperty("upgrade_hand", out var upgradeEl) && upgradeEl.GetBoolean())
                foreach (var card in pcs.Hand.Cards.Where(c => c.IsUpgradable).ToList())
                {
                    card.UpgradeInternal();
                    card.FinalizeUpgradeInternal();
                }
            SetField(player.Creature, "_currentHp", hp);
            SetField(pcs, "_energy", energy);
            if (enemyHp != null)
                for (var i = 0; i < enemies.Count; i++)
                {
                    SetField(enemies[i], "_maxHp", Math.Max(enemies[i].MaxHp, enemyHp[i]));
                    SetField(enemies[i], "_currentHp", enemyHp[i]);
                }
            _syncCtx.Pump();
            WaitForActionExecutor();
            return DetectDecisionPoint();
        }
        catch (Exception ex) { return ErrorWithTrace("ConfigureSandbox failed", ex); }
    }

    public Dictionary<string, object?> SetDrawOrder(List<string> cardIds)
    {
        try
        {
            if (_runState == null) return Error("No run in progress");
            var player = _runState.Players[0];
            var pcs = player.PlayerCombatState;
            if (pcs?.DrawPile == null) return Error("Not in combat");

            var drawList = GetBackingList<CardModel>(pcs.DrawPile, "_cards");
            if (drawList == null) return Error("Cannot access draw pile");

            var newOrder = new List<CardModel>();
            var available = new List<CardModel>(drawList);
            foreach (var cardId in cardIds)
            {
                var match = available.FirstOrDefault(c =>
                    c.Id.Entry.Equals(cardId, StringComparison.OrdinalIgnoreCase));
                if (match != null)
                {
                    newOrder.Add(match);
                    available.Remove(match);
                }
            }
            newOrder.AddRange(available);

            drawList.Clear();
            drawList.AddRange(newOrder);

            Log($"SetDrawOrder: {newOrder.Count} cards, top={newOrder.FirstOrDefault()?.Id.Entry}");
            return new Dictionary<string, object?>
            {
                ["type"] = "ok",
                ["draw_pile_count"] = drawList.Count,
                ["top_cards"] = newOrder.Take(5).Select(c => _loc.Card(c.Id.Entry)).ToList(),
            };
        }
        catch (Exception ex) { return ErrorWithTrace("SetDrawOrder failed", ex); }
    }

    // ─── Game actions ───
    public Dictionary<string, object?> LoadSave(string saveJson, string lang = "en", bool resumeLatestRoom = false)
    {
        try
        {
            _loc.Lang = lang;
            EnsureModelDbInitialized();

            Log("Loading save file...");

            if (!ValidateSaveSchemaVersion(saveJson, out var schemaError))
                return Error($"Save schema mismatch: {schemaError}");

            var readResult = SaveManager.FromJson<SerializableRun>(saveJson);
            if (!readResult.Success || readResult.SaveData == null)
                return Error($"Failed to parse save file: {readResult.Status} {readResult.ErrorMessage}");

            var save = readResult.SaveData;
            Log($"Save loaded: seed={save.SerializableRng?.Seed}, act={save.CurrentActIndex}, ascension={save.Ascension}");

            // A persistent shadow may re-anchor from a new authoritative save
            // without restarting its process. Match StartRun/StartTestCombat's
            // lifecycle boundary so RunManager never retains the prior run.
            if (_runState != null || RunManager.Instance.DebugOnlyGetState() != null || RunManager.Instance.IsInProgress)
                CleanUp(keepProcessAlive: true);

            ResetTreasureInteractionState();
            _runState = RunState.FromSerializable(save);
            if (_runState == null)
                return Error("Failed to create RunState from save");

            Log($"RunState created, players={_runState.Players?.Count}");

            var netService = new NetSingleplayerGameService();
            RunManager.Instance.SetUpSavedSingleplayer(_runState, save).GetAwaiter().GetResult();
            LocalContext.NetId = netService.NetId;

            RegisterCombatEventHandlers();
            EnsureCardSelectorInstalled();

            var savedRoom = _runState.CurrentRoom;

            // Save visited coords before Launch (EnterAct will clear them)
            var savedVisitedCoords = _runState.VisitedMapCoords?.ToList() ?? new List<MapCoord>();
            var savedMapPointHistory = _runState.MapPointHistory
                .Select(entries => entries.ToList()).ToList();
            void RestoreSavedMapPointHistory(bool excludeCurrentRoom)
            {
                if (savedVisitedCoords.Count == 0)
                    return;
                var history = GetBackingList<List<MapPointHistoryEntry>>(_runState, "_mapPointHistory")
                    ?? throw new InvalidOperationException("Run has no map point history storage");
                var completedHistoryCount = savedMapPointHistory.Count;
                if (excludeCurrentRoom && completedHistoryCount > 0)
                    completedHistoryCount--;
                history.Clear();
                history.AddRange(savedMapPointHistory.Take(completedHistoryCount));
                Log($"Restored {completedHistoryCount} completed map history groups"
                    + (excludeCurrentRoom ? "; current room remains owned by native room restoration" : ""));
            }
            var shouldResumeInitialNeow = IsInitialNeowSave(saveJson);
            Log($"Save has {savedVisitedCoords.Count} visited coords");

            RunManager.Instance.Launch();
            Log("Run launched");

            if (savedRoom is MapRoom || savedRoom == null)
            {
                // Preserve Neow for saves created before the first blessing choice.
                // Once the run has visited at least one map node, re-entering Act 1
                // should not send the player back through the Ancient start node.
                if (_runState.CurrentActIndex == 0 && savedVisitedCoords.Count > 0)
                    _runState.ExtraFields.StartedWithNeow = false;
                RunManager.Instance.EnterAct(_runState.CurrentActIndex, doTransition: false).GetAwaiter().GetResult();
                _syncCtx.Pump();
                Log($"Entered Act {_runState.CurrentActIndex}");

                // EnterAct resets unknown-room odds for a NEW act. A restored
                // mid-act save must retain its accumulated native probabilities.
                var savedOdds = save.SerializableOdds;
                var odds = _runState.Odds.UnknownMapPoint;
                odds.MonsterOdds = savedOdds.UnknownMapPointMonsterOddsValue;
                odds.EliteOdds = savedOdds.UnknownMapPointEliteOddsValue;
                odds.TreasureOdds = savedOdds.UnknownMapPointTreasureOddsValue;
                odds.ShopOdds = savedOdds.UnknownMapPointShopOddsValue;

                if (shouldResumeInitialNeow && _runState.Map?.StartingMapPoint != null)
                {
                    Log("Restoring initial Neow event");
                    RunManager.Instance.EnterMapCoord(_runState.Map.StartingMapPoint.coord).GetAwaiter().GetResult();
                    _syncCtx.Pump();
                }

                // EnterAct clears visited coords and ActFloor — restore them from save
                if (savedVisitedCoords.Count > 0)
                {
                    if (_runState.VisitedMapCoords == null || _runState.VisitedMapCoords.Count == 0)
                    {
                        foreach (var coord in savedVisitedCoords)
                            _runState.AddVisitedMapCoord(coord);
                    }
                    _runState.ActFloor = savedVisitedCoords.Count;
                    // EnterAct notified its new-act/null position. We restored
                    // the visited coordinates without entering a room: finish
                    // that restoration with the native notification for BOTH
                    // map voting and location-targeted messages. Room resume
                    // calls EnterMapCoordInternal, which owns this notification.
                    if (!resumeLatestRoom)
                    {
                        var notify = typeof(RunManager).GetMethod("AfterMapLocationChanged", NonPublic)
                            ?? throw new InvalidOperationException("Native map location lifecycle is unavailable");
                        notify.Invoke(RunManager.Instance, null);
                    }
                    var last = savedVisitedCoords[^1];
                    Log($"Restored map position: floor={_runState.ActFloor}, coord=({last.col},{last.row})");

                }
            }
            else
            {
                Log($"Preserving saved room: {savedRoom.GetType().Name}");
            }

            if (resumeLatestRoom && !shouldResumeInitialNeow)
            {
                RunManager.Instance.LoadIntoLatestMapCoord(savedRoom).GetAwaiter().GetResult();
                _syncCtx.Pump();
                WaitForActionExecutor();

                // The serialized room counters already include the room being
                // resumed. LoadIntoLatestMapCoord enters that room through the
                // native path and increments its counter again (for example,
                // eventsVisited 1 -> 2 when restoring a completed Neow map
                // anchor). Restore the serialized counters after native room
                // construction so the next event/encounter is the same one the
                // visible run will select.
                RestoreSavedRoomProgress(save);
            }
            // Serialized history includes the currently active room. Native
            // LoadIntoLatestMapCoord owns that room and will complete it once;
            // restoring the serialized current entry as completed history made
            // the next encounter derive its local seed from an extra floor.
            // Map-boundary saves have no active room, so all saved groups are
            // already completed and must remain intact.
            var restoredActiveRoom = resumeLatestRoom && !shouldResumeInitialNeow
                && savedRoom is not MapRoom && savedRoom != null;
            RestoreSavedMapPointHistory(restoredActiveRoom);

            return DetectDecisionPoint();
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("LoadSave failed", ex);
        }
    }

    private void RestoreSavedRoomProgress(SerializableRun save)
    {
        if (_runState == null)
            return;

        var count = Math.Min(_runState.Acts.Count, save.Acts.Count);
        var roomsField = typeof(ActModel).GetField("_rooms", NonPublic)
            ?? throw new InvalidOperationException("ActModel room storage is unavailable");
        for (var i = 0; i < count; i++)
        {
            if (roomsField.GetValue(_runState.Acts[i]) is not RoomSet runtimeRooms)
                throw new InvalidOperationException($"Act {i} room storage is unavailable");
            var savedRooms = save.Acts[i].SerializableRooms;
            SetField(runtimeRooms, "eventsVisited", savedRooms.EventsVisited);
            SetField(runtimeRooms, "normalEncountersVisited", savedRooms.NormalEncountersVisited);
            SetField(runtimeRooms, "eliteEncountersVisited", savedRooms.EliteEncountersVisited);
            SetField(runtimeRooms, "bossEncountersVisited", savedRooms.BossEncountersVisited);
        }
    }

    /// <summary>
    /// Expected run save <c>schema_version</c> (lazy: first load_save only, so StartRun never fails on reflection).
    /// Order: <c>STS2_SAVE_SCHEMA_VERSION</c> env → reflect sts2.dll → unknown, defer to SaveManager.
    /// </summary>
    private static int? GetExpectedSaveSchemaVersion()
    {
        if (_expectedSaveSchemaVersionReady)
            return _expectedSaveSchemaVersion;
        lock (_expectedSaveSchemaVersionLock)
        {
            if (_expectedSaveSchemaVersionReady)
                return _expectedSaveSchemaVersion;
            _expectedSaveSchemaVersion = ResolveExpectedSaveSchemaVersion();
            _expectedSaveSchemaVersionReady = true;
            return _expectedSaveSchemaVersion;
        }
    }

    private static int? ResolveExpectedSaveSchemaVersion()
    {
        var env = Environment.GetEnvironmentVariable("STS2_SAVE_SCHEMA_VERSION");
        if (!string.IsNullOrWhiteSpace(env) && int.TryParse(env.Trim(), out var envVer))
            return envVer;

        var reflected = TryReflectLatestSaveSchemaVersion();
        if (reflected.HasValue)
            return reflected.Value;

        Console.Error.WriteLine(
            "[Sts2Headless] Could not read save schema from sts2.dll; deferring schema compatibility " +
            "to SaveManager.FromJson. Set STS2_SAVE_SCHEMA_VERSION to enforce a specific version.");
        return null;
    }

    /// <summary>Find static parameterless GetLatestSchemaVersion (or close) on sts2; supports int/uint/long.</summary>
    private static int? TryReflectLatestSaveSchemaVersion()
    {
        var asm = typeof(SerializableRun).Assembly;
        Type[] types;
        try
        {
            types = asm.GetTypes();
        }
        catch (ReflectionTypeLoadException ex)
        {
            types = ex.Types.Where(t => t != null).Cast<Type>().ToArray();
        }

        var candidates = new List<(int score, string typeName, int value)>();
        foreach (var t in types)
        {
            MethodInfo? m;
            try
            {
                foreach (var name in new[] { "GetLatestSchemaVersion", "GetLatestVersion" })
                {
                    m = t.GetMethod(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                        null, Type.EmptyTypes, null);
                    if (m == null) continue;
                    var tn = t.FullName ?? "";
                    // Avoid unrelated static GetLatestVersion() elsewhere in the assembly.
                    if (name == "GetLatestVersion" && !tn.Contains("Saves", StringComparison.Ordinal))
                        continue;

                    var conv = TryConvertSchemaNumber(m.Invoke(null, null));
                    if (!conv.HasValue) continue;

                    var score = name == "GetLatestSchemaVersion" ? 100 : 0;
                    if (tn.Contains("Saves", StringComparison.Ordinal)) score += 50;
                    if (tn.Contains("Schema", StringComparison.Ordinal) || tn.Contains("Migration", StringComparison.Ordinal))
                        score += 25;
                    candidates.Add((score, tn, conv.Value));
                }
            }
            catch
            {
                // type may not support full reflection on this runtime
            }
        }

        if (candidates.Count == 0)
            return null;

        var best = candidates.OrderByDescending(c => c.score).ThenBy(c => c.typeName).First();
        return best.value;
    }

    private static int? TryConvertSchemaNumber(object? value) => value switch
    {
        int i => i,
        uint u => u <= int.MaxValue ? (int)u : null,
        long l => l >= int.MinValue && l <= int.MaxValue ? (int)l : null,
        short s => s,
        ushort us => us,
        byte b => b,
        _ => null,
    };

    private static bool ValidateSaveSchemaVersion(string saveJson, out string error)
    {
        error = "";
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(saveJson);
            var root = doc.RootElement;

            if (!root.TryGetProperty("schema_version", out var versionElem))
            {
                error = "missing schema_version";
                return false;
            }

            if (versionElem.ValueKind != System.Text.Json.JsonValueKind.Number ||
                !versionElem.TryGetInt32(out var schemaVersion))
            {
                error = "schema_version is not a valid integer";
                return false;
            }

            var expected = GetExpectedSaveSchemaVersion();
            if (expected.HasValue && schemaVersion != expected.Value)
            {
                error = $"expected v{expected.Value}, got v{schemaVersion}";
                return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            error = $"could not inspect save: {ex.Message}";
            return false;
        }
    }

    private static bool TrySetPropertyValue(object target, string propertyName, object? value)
    {
        var prop = target.GetType().GetProperty(propertyName);
        if (prop?.CanWrite != true)
            return false;
        prop.SetValue(target, value);
        return true;
    }

    private static bool IsInitialNeowSave(string saveJson)
    {
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(saveJson);
            var root = doc.RootElement;

            if (!root.TryGetProperty("current_act_index", out var actIndexElem) || actIndexElem.GetInt32() != 0)
                return false;

            var hasVisitedCoords = root.TryGetProperty("visited_map_coords", out var visitedElem)
                                && visitedElem.ValueKind == System.Text.Json.JsonValueKind.Array
                                && visitedElem.GetArrayLength() > 0;
            if (hasVisitedCoords)
                return false;

            return root.TryGetProperty("extra_fields", out var extraFieldsElem)
                && extraFieldsElem.ValueKind == System.Text.Json.JsonValueKind.Object
                && extraFieldsElem.TryGetProperty("started_with_neow", out var startedElem)
                && startedElem.ValueKind == System.Text.Json.JsonValueKind.True;
        }
        catch
        {
            return false;
        }
    }

    private static bool TryRollbackSerializedSaveToPreRoom(SerializableRun serializableRun, out string error)
    {
        error = "";

        var saveType = serializableRun.GetType();
        var visitedProp = saveType.GetProperty("VisitedMapCoords");
        if (visitedProp == null)
        {
            error = "Save data is missing VisitedMapCoords";
            return false;
        }

        var visitedValue = visitedProp.GetValue(serializableRun);
        var visitedItems = new List<object?>();
        if (visitedValue is System.Collections.IEnumerable visitedEnumerable)
        {
            foreach (var item in visitedEnumerable)
                visitedItems.Add(item);
        }

        if (visitedItems.Count == 0)
        {
            error = "Cannot roll back save before the first room";
            return false;
        }

        visitedItems.RemoveAt(visitedItems.Count - 1);

        var visitedType = visitedProp.PropertyType;
        if (visitedType.IsArray)
        {
            var elementType = visitedType.GetElementType()!;
            var array = Array.CreateInstance(elementType, visitedItems.Count);
            for (int i = 0; i < visitedItems.Count; i++)
                array.SetValue(visitedItems[i], i);
            visitedProp.SetValue(serializableRun, array);
        }
        else if (visitedType.IsGenericType)
        {
            var elementType = visitedType.GetGenericArguments()[0];
            var listType = typeof(List<>).MakeGenericType(elementType);
            var list = (System.Collections.IList)Activator.CreateInstance(listType)!;
            foreach (var item in visitedItems)
                list.Add(item);
            visitedProp.SetValue(serializableRun, list);
        }
        else
        {
            error = $"Unsupported VisitedMapCoords type: {visitedType.Name}";
            return false;
        }

        TrySetPropertyValue(serializableRun, "ActFloor", visitedItems.Count);
        TrySetPropertyValue(serializableRun, "CurrentMapCoord", visitedItems.Count > 0 ? visitedItems[^1] : null);
        TrySetPropertyValue(serializableRun, "PreFinishedRoom", null);
        TrySetPropertyValue(serializableRun, "CurrentRoom", null);
        return true;
    }

    // Root-cause workaround for a lazy-initialization defect in the compiled
    // game core: the FIRST Player.ToSerializable() call after entering a room
    // throws NullReferenceException on an uninitialized lazy field, but the
    // failed call itself completes that initialization, so an immediate retry
    // succeeds (verified empirically: save #1 NREs, saves #2..n succeed and are
    // byte-identical). Every save path (exact, continue, map prefetch) funnels
    // through RunManager.ToSave, so warming up here fixes all of them. The
    // retry re-throws any NRE that survives initialization, so a genuine
    // serialization fault still surfaces instead of being masked.
    private SerializableRun ToSaveWithWarmup(AbstractRoom? room)
    {
        try
        {
            return RunManager.Instance.ToSave(room);
        }
        catch (NullReferenceException)
        {
            Log("ToSave: first-call lazy-init NRE caught, retrying once (warmup)");
            return RunManager.Instance.ToSave(room);
        }
    }

    public Dictionary<string, object?> SaveCheckpoint(string? outputPath)
    {
        try
        {
            if (_runState == null)
                return Error("No active run to save");

            if (string.IsNullOrEmpty(outputPath))
                return Error("No output path specified for quit save");

            var currentRoom = _runState.CurrentRoom;
            SerializableRun serializableRun;

            if (currentRoom is MapRoom || currentRoom == null)
            {
                Log($"Saving map checkpoint (room={currentRoom?.GetType().Name ?? "null"}, outputPath={outputPath})...");
                serializableRun = ToSaveWithWarmup(currentRoom);
            }
            else
            {
                Log($"Saving pre-room checkpoint from {currentRoom.GetType().Name} (outputPath={outputPath})...");
                serializableRun = ToSaveWithWarmup(new MapRoom());
                if (!TryRollbackSerializedSaveToPreRoom(serializableRun, out var rollbackError))
                    return Error($"Cannot save checkpoint: {rollbackError}");
            }

            var saveJson = SaveManager.ToJson(serializableRun);
            Log($"Serialized save: {saveJson.Length} chars");

            var dir = System.IO.Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(dir))
                System.IO.Directory.CreateDirectory(dir);
            System.IO.File.WriteAllText(outputPath, saveJson);
            Log($"Save written to: {outputPath}");

            return new Dictionary<string, object?>
            {
                ["type"] = "save_result",
                ["success"] = true,
                ["path"] = outputPath,
                ["size"] = saveJson.Length,
                ["room_type"] = currentRoom?.GetType().Name,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("SaveCheckpoint failed", ex);
        }
    }

    public Dictionary<string, object?> SaveExactState(string? outputPath)
    {
        try
        {
            if (_runState == null)
                return Error("No active run to save");

            if (string.IsNullOrEmpty(outputPath))
                return Error("No output path specified for exact save");

            var currentRoom = _runState.CurrentRoom;
            Log($"Saving exact state (room={currentRoom?.GetType().Name ?? "null"}, outputPath={outputPath})...");
            var serializableRun = ToSaveWithWarmup(currentRoom);

            var saveJson = SaveManager.ToJson(serializableRun);
            Log($"Serialized exact save: {saveJson.Length} chars");

            var dir = System.IO.Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(dir))
                System.IO.Directory.CreateDirectory(dir);
            System.IO.File.WriteAllText(outputPath, saveJson);
            Log($"Exact save written to: {outputPath}");

            return new Dictionary<string, object?>
            {
                ["type"] = "save_result",
                ["success"] = true,
                ["path"] = outputPath,
                ["size"] = saveJson.Length,
                ["room_type"] = currentRoom?.GetType().Name,
                ["save_mode"] = "exact",
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("SaveExactState failed", ex);
        }
    }

    public Dictionary<string, object?> ExecuteAction(string action, Dictionary<string, object?>? args)
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");

            var player = _runState.Players[0];

            switch (action)
            {
                case "select_map_node":
                    return DoMapSelect(player, args);
                case "play_card":
                    return DoPlayCard(player, args);
                case "end_turn":
                    return DoEndTurn(player);
                case "choose_option":
                    return DoChooseOption(player, args);
                case "crystal_sphere_divine":
                    return DoCrystalSphereDivine(args);
                case "reconcile_relics":
                    return DoReconcileRelics(player, args);
                case "select_card_reward":
                    return DoSelectCardReward(player, args);
                case "skip_card_reward":
                    return DoSkipCardReward(player);
                    case "claim_combat_reward":
                        return DoClaimCombatReward(player, args);
                    case "ack_event_reward":
                        return DoAckEventReward(player, args);
                case "finish_combat_rewards":
                    return DoFinishCombatRewards(player);
                case "buy_card":
                    return DoBuyCard(player, args);
                case "buy_relic":
                    return DoBuyRelic(player, args);
                case "buy_potion":
                    return DoBuyPotion(player, args);
                case "remove_card":
                    return DoRemoveCard(player);
                case "select_bundle":
                    return DoSelectBundle(player, args);
                case "select_cards":
                    return DoSelectCards(player, args);
                case "skip_select":
                    return DoSkipSelect(player);
                case "use_potion":
                    return DoUsePotion(player, args);
                case "discard_potion":
                    return DoDiscardPotion(player, args);
                case "open_chest":
                    return DoOpenChest(player);
                case "choose_treasure_relic":
                    return DoChooseTreasureRelic(player, args);
                case "leave_room":
                    return DoLeaveRoom(player);
                case "proceed":
                    return DoProceed(player);
                default:
                    return Error($"Unknown action: {action}");
            }
        }
        catch (Exception ex)
        {
            return ErrorWithTrace($"Action '{action}' failed", ex);
        }
    }

    public Dictionary<string, object?> ExecuteActionWithEngineSnapshot(string action, Dictionary<string, object?>? args)
    {
        var actionResult = ExecuteAction(action, args);

        // Keep behavior transparent: always return the original action result;
        // add engine snapshot when combat is active after the action resolves.
        var wrapped = new Dictionary<string, object?>
        {
            ["type"] = "action_with_engine_snapshot_result",
            ["action"] = action,
            ["result"] = actionResult,
        };

        try
        {
            if (_runState != null && CombatManager.Instance.IsInProgress)
            {
                var snapshotResult = CaptureEngineCombatSnapshot();
                wrapped["engine_snapshot_result"] = snapshotResult;
                wrapped["transition_diff"] = BuildTransitionDiff(actionResult, snapshotResult);
                wrapped["combat_state_for_search"] = BuildCombatStateForSearch(actionResult, snapshotResult);
            }
            else
            {
                wrapped["engine_snapshot_result"] = new Dictionary<string, object?>
                {
                    ["type"] = "engine_combat_snapshot",
                    ["success"] = false,
                    ["message"] = "Not in combat after action",
                };
            }
        }
        catch (Exception ex)
        {
            wrapped["engine_snapshot_result"] = new Dictionary<string, object?>
            {
                ["type"] = "engine_combat_snapshot",
                ["success"] = false,
                ["message"] = $"Snapshot capture failed: {ex.Message}",
            };
        }

        return wrapped;
    }

    private Dictionary<string, object?> BuildCombatStateForSearch(
        Dictionary<string, object?> actionResult,
        Dictionary<string, object?> snapshotResult)
    {
        var state = new Dictionary<string, object?>
        {
            ["schema_version"] = "search_state_v1",
            ["success"] = false,
        };

        if (!actionResult.TryGetValue("decision", out var dObj) || !string.Equals(dObj as string, "combat_play", StringComparison.Ordinal))
        {
            state["message"] = "Action result is not combat_play decision";
            return state;
        }
        if (!snapshotResult.TryGetValue("success", out var sObj) || sObj is not bool ok || !ok)
        {
            state["message"] = "Engine snapshot unavailable";
            return state;
        }

        var player = _runState?.Players?.FirstOrDefault();
        var pcs = player?.PlayerCombatState;
        var combatState = CombatManager.Instance.DebugOnlyGetState();
        var snapshot = snapshotResult.TryGetValue("snapshot", out var snapObj) ? snapObj : null;
        var players = GetMember(snapshot, "Players") as System.Collections.IEnumerable;
        var creatures = GetMember(snapshot, "Creatures") as System.Collections.IEnumerable;
        var playerState = players?.Cast<object?>().FirstOrDefault();
        if (playerState == null || creatures == null)
        {
            state["message"] = "Missing player/creature data";
            return state;
        }

        var playerCreature = creatures.Cast<object?>().FirstOrDefault(c => GetField(c, "playerId") != null);
        var enemyCreatures = creatures.Cast<object?>().Where(c => GetField(c, "playerId") == null).ToList();

        List<object?> NormalizeCardPile(System.Collections.IEnumerable? cards, bool sortForVisibility = false)
        {
            var normalized = (cards?.Cast<object?>() ?? Enumerable.Empty<object?>())
                .Where(c => c != null)
                .Select(c =>
                {
                    var keywordsObj = GetMember(c, "Keywords") as System.Collections.IEnumerable;
                    var keywords = keywordsObj?.Cast<object?>()
                        .Where(k => k != null && !string.Equals(k.ToString(), CardKeyword.None.ToString(), StringComparison.Ordinal))
                        .Select(k => k?.ToString())
                        .Where(k => !string.IsNullOrWhiteSpace(k))
                        .Cast<object?>()
                        .ToList();

                    string? affliction = null;
                    int? afflictionCount = null;
                    try
                    {
                        var afflictionObj = GetMember(c, "Affliction");
                        if (afflictionObj != null)
                        {
                            affliction = EntryOf(afflictionObj);
                            var amountObj = GetMember(afflictionObj, "Amount");
                            if (amountObj != null)
                                afflictionCount = Convert.ToInt32(amountObj);
                        }
                    }
                    catch { }

                    return new Dictionary<string, object?>
                    {
                        ["card_id"] = EntryOf(c),
                        ["upgrade"] = Convert.ToInt32(GetMember(c, "CurrentUpgradeLevel") ?? 0),
                        ["current_cost"] = GetMember(GetMember(c, "EnergyCost"), "ResolvedValue") ?? GetMember(GetMember(c, "EnergyCost"), "Value"),
                        ["display_cost"] = c is CardModel cardModel ? cardModel.EnergyCost?.GetWithModifiers((CostModifiers)(-1)) : null,
                        ["display_costs_x"] = c is CardModel xCard ? xCard.EnergyCost?.CostsX : null,
                        ["keywords"] = keywords?.Count > 0 ? keywords : null,
                        ["affliction"] = affliction,
                        ["affliction_count"] = afflictionCount,
                    };
                })
                .Cast<object?>()
                .ToList();

            if (sortForVisibility)
            {
                normalized = normalized
                    .OfType<Dictionary<string, object?>>()
                    .OrderBy(c => c.GetValueOrDefault("card_id")?.ToString() ?? "")
                    .ThenBy(c => Convert.ToInt32(c.GetValueOrDefault("upgrade") ?? 0))
                    .ThenBy(c => Convert.ToInt32(c.GetValueOrDefault("current_cost") ?? 0))
                    .Cast<object?>()
                    .ToList();
            }

            return normalized;
        }

        List<object?> NormalizePowers(System.Collections.IEnumerable? powers)
        {
            if (powers == null) return new List<object?>();
            return powers.Cast<object?>().Where(p => p != null).Select(p => new Dictionary<string, object?>
            {
                ["id"] = EntryOf(p),
                ["amount"] = Convert.ToInt32(GetMember(p, "Amount") ?? 0),
                ["extra"] = null,
            }).Cast<object?>().ToList();
        }

        string? EntryOf(object? obj) => GetMember(GetMember(obj, "Id"), "Entry")?.ToString() ?? GetMember(obj, "Entry")?.ToString();

        Dictionary<string, object?>? BuildIntentState(Creature enemy)
        {
            try
            {
                var intents = enemy.Monster?.NextMove?.Intents?.ToList();
                if (intents == null) return null;

                var intentTypes = intents.Select(i => i.IntentType.ToString()).ToList();
                int? displayDamage = null;
                int? hits = null;
                var firstAttack = intents.OfType<MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent>().FirstOrDefault();
                if (firstAttack != null && combatState?.PlayerCreatures != null)
                {
                    try
                    {
                        displayDamage = firstAttack.GetTotalDamage(combatState.PlayerCreatures.ToList(), enemy);
                        if (firstAttack.Repeats > 1)
                            hits = firstAttack.Repeats;
                    }
                    catch { }
                }

                return new Dictionary<string, object?>
                {
                    ["intent_types"] = intentTypes,
                    ["total_damage"] = displayDamage,
                    ["display_damage"] = displayDamage,
                    ["hits"] = hits,
                };
            }
            catch
            {
                return null;
            }
        }

        Dictionary<string, object?> BuildCardActionMetadata(CardModel card, Creature? target = null)
        {
            var metadata = NormalizeCardPile(new[] { card })
                .OfType<Dictionary<string, object?>>()
                .FirstOrDefault() ?? new Dictionary<string, object?> { ["card_id"] = card.Id.Entry };
            metadata["target_type"] = card.TargetType.ToString();
            if (target != null)
                metadata["target_monster_id"] = target.Monster?.Id.Entry;
            return metadata;
        }

        List<object?> BuildAvailableActions(PlayerCombatState? playerCombatState, IReadOnlyList<Creature>? enemiesInCombat)
        {
            var actions = new List<object?>();
            if (playerCombatState == null) return actions;

            actions.Add(new Dictionary<string, object?>
            {
                ["action_type"] = "end_turn",
                ["card_index"] = null,
                ["target_index"] = null,
                ["metadata"] = null,
            });

            var liveEnemies = enemiesInCombat?.Where(e => e != null && e.IsAlive).ToList() ?? new List<Creature>();
            foreach (var card in playerCombatState.Hand?.Cards?.Select((c, i) => (card: c, index: i)) ?? Enumerable.Empty<(CardModel card, int index)>())
            {
                if (!card.card.CanPlay(out _, out _))
                    continue;

                if (card.card.TargetType == TargetType.AnyEnemy && liveEnemies.Count > 0)
                {
                    for (var targetIndex = 0; targetIndex < liveEnemies.Count; targetIndex++)
                    {
                        actions.Add(new Dictionary<string, object?>
                        {
                            ["action_type"] = "play_card",
                            ["card_index"] = card.index,
                            ["target_index"] = targetIndex,
                            ["metadata"] = BuildCardActionMetadata(card.card, liveEnemies[targetIndex]),
                        });
                    }
                }
                else
                {
                    actions.Add(new Dictionary<string, object?>
                    {
                        ["action_type"] = "play_card",
                        ["card_index"] = card.index,
                        ["target_index"] = null,
                        ["metadata"] = BuildCardActionMetadata(card.card),
                    });
                }
            }

            var potions = player?.Potions?.Cast<object?>().ToList() ?? new List<object?>();
            for (var potionIndex = 0; potionIndex < potions.Count; potionIndex++)
            {
                var potion = potions[potionIndex];
                if (potion == null) continue;

                var targetType = GetMember(potion, "TargetType")?.ToString();
                var potionId = EntryOf(potion);

                if (string.Equals(targetType, TargetType.AnyEnemy.ToString(), StringComparison.Ordinal) && liveEnemies.Count > 0)
                {
                    for (var targetIndex = 0; targetIndex < liveEnemies.Count; targetIndex++)
                    {
                        actions.Add(new Dictionary<string, object?>
                        {
                            ["action_type"] = "use_potion",
                            ["card_index"] = null,
                            ["target_index"] = targetIndex,
                            ["metadata"] = new Dictionary<string, object?>
                            {
                                ["potion_index"] = potionIndex,
                                ["potion_id"] = potionId,
                                ["target_type"] = targetType,
                                ["target_monster_id"] = liveEnemies[targetIndex].Monster?.Id.Entry,
                            },
                        });
                    }
                }
                else
                {
                    actions.Add(new Dictionary<string, object?>
                    {
                        ["action_type"] = "use_potion",
                        ["card_index"] = null,
                        ["target_index"] = null,
                        ["metadata"] = new Dictionary<string, object?>
                        {
                            ["potion_index"] = potionIndex,
                            ["potion_id"] = potionId,
                            ["target_type"] = targetType,
                        },
                    });
                }

                actions.Add(new Dictionary<string, object?>
                {
                    ["action_type"] = "discard_potion",
                    ["card_index"] = null,
                    ["target_index"] = null,
                    ["metadata"] = new Dictionary<string, object?>
                    {
                        ["potion_index"] = potionIndex,
                        ["potion_id"] = potionId,
                    },
                });
            }

            return actions;
        }

        var liveEnemies = OrderEnemiesForSearch(combatState?.Enemies?.Where(e => e != null && e.IsAlive).ToList() ?? new List<Creature>());
        var enemies = liveEnemies.Select((e, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["monster_id"] = e.Monster?.Id.Entry ?? EntryOf(GetField(enemyCreatures.ElementAtOrDefault(i), "monsterId")),
            ["hp"] = e.CurrentHp,
            ["max_hp"] = e.MaxHp,
            ["block"] = e.Block,
            ["powers"] = NormalizePowers(e.Powers),
            ["intent"] = BuildIntentState(e),
        }).Cast<object?>().ToList();

        var playerPowers = player?.Creature?.Powers;
        var relics = player?.Relics?
            .Select(r => new Dictionary<string, object?>
            {
                ["id"] = r.Id.Entry,
                ["extra"] = null,
            })
            .Cast<object?>()
            .ToList() ?? new List<object?>();

        state["success"] = true;
        state["character"] = (player?.Character?.Id.Entry ?? EntryOf(GetField(playerState, "characterId")) ?? "IRONCLAD").ToUpperInvariant();
        state["combat"] = new Dictionary<string, object?>
        {
            ["encounter_id"] = _runState?.CurrentRoom is CombatRoom activeRoom
                ? activeRoom.Encounter?.Id.Entry
                : null,
            ["turn_number"] = GetMember(combatState, "TurnNumber") ?? combatState?.RoundNumber ?? ToInt(actionResult, "round"),
            ["round_number"] = combatState?.RoundNumber ?? ToInt(actionResult, "round"),
            ["is_player_turn"] = IsPlayPhase(),
            ["player"] = new Dictionary<string, object?>
            {
                ["hp"] = player?.Creature?.CurrentHp ?? GetIntField(playerCreature, "currentHp"),
                ["max_hp"] = player?.Creature?.MaxHp ?? GetIntField(playerCreature, "maxHp"),
                ["block"] = player?.Creature?.Block ?? GetIntField(playerCreature, "block"),
                ["energy"] = pcs?.Energy ?? GetIntField(playerState, "energy"),
                ["powers"] = NormalizePowers(playerPowers),
                ["relics"] = relics,
            },
            ["enemies"] = enemies,
            ["hand"] = NormalizeCardPile(pcs?.Hand?.Cards),
            // Search state must preserve the live draw order. Sorting here makes
            // every rollout see a different top card than the engine will draw.
            ["draw_pile"] = NormalizeCardPile(pcs?.DrawPile?.Cards),
            ["discard_pile"] = NormalizeCardPile(pcs?.DiscardPile?.Cards),
            ["exhaust_pile"] = NormalizeCardPile(pcs?.ExhaustPile?.Cards),
            ["play_pile"] = NormalizeCardPile(GetField((GetField(playerState, "piles") as System.Collections.IEnumerable)?
                .Cast<object?>()
                .FirstOrDefault(p => string.Equals(GetField(p, "pileType")?.ToString(), "Play", StringComparison.OrdinalIgnoreCase)), "cards") as System.Collections.IEnumerable),
            ["available_actions"] = BuildAvailableActions(pcs, liveEnemies),
        };
        return state;
    }

    public Dictionary<string, object?> CaptureEngineCombatSnapshot()
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");
            if (!CombatManager.Instance.IsInProgress)
                return Error("Not in combat");

            var snapshot = NetFullCombatState.FromRun(_runState, justFinishedAction: null!);
            if (snapshot == null)
                return Error("NetFullCombatState.FromRun returned null");

            var creatures = snapshot.Creatures?.Count ?? 0;
            var players = snapshot.Players?.Count ?? 0;
            return new Dictionary<string, object?>
            {
                ["type"] = "engine_combat_snapshot",
                ["success"] = true,
                ["creature_count"] = creatures,
                ["player_count"] = players,
                ["snapshot"] = snapshot,
                ["snapshot_plain"] = ToPlainObject(snapshot),
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("CaptureEngineCombatSnapshot failed", ex);
        }
    }

    public Dictionary<string, object?> GetCurrentSearchState()
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");
            if (!CombatManager.Instance.IsInProgress)
                return Error("Not in combat");

            var player = _runState.Players.FirstOrDefault();
            if (player == null)
                return Error("No player available");

            var searchState = BuildLiveCombatSearchState();
            return new Dictionary<string, object?>
            {
                ["type"] = "search_state_result",
                ["success"] = searchState.TryGetValue("success", out var sObj) && sObj is bool ok && ok,
                ["combat_state_for_search"] = searchState,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("GetCurrentSearchState failed", ex);
        }
    }

    public Dictionary<string, object?> InspectEnemyAi()
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");
            if (!CombatManager.Instance.IsInProgress)
                return Error("Not in combat");

            var combatState = CombatManager.Instance.DebugOnlyGetState();
            if (combatState == null)
                return Error("Combat state unavailable");

            var enemies = combatState.Enemies?.Where(e => e != null && e.IsAlive).ToList() ?? new List<Creature>();
            static object? AnyMember(object? obj, string name)
            {
                if (obj == null) return null;
                var t = obj.GetType();
                var p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (p != null) return p.GetValue(obj);
                var f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (f != null) return f.GetValue(obj);
                var backing = t.GetField($"<{name}>k__BackingField", BindingFlags.Instance | BindingFlags.NonPublic);
                if (backing != null) return backing.GetValue(obj);
                return null;
            }
            var result = new List<object?>();
            foreach (var enemy in enemies)
            {
                var monster = enemy.Monster;
                var sm = monster?.MoveStateMachine;
                var states = AnyMember(sm, "States") as System.Collections.IDictionary;
                var stateLog = AnyMember(sm, "StateLog") as System.Collections.IEnumerable;
                var currentState = AnyMember(sm, "_currentState") ?? AnyMember(sm, "CurrentState");
                var initialState = AnyMember(sm, "_initialState") ?? AnyMember(sm, "InitialState");
                var reverse = new Dictionary<object, string>(ReferenceEqualityComparer.Instance);
                if (states != null)
                {
                    foreach (System.Collections.DictionaryEntry entry in states)
                    {
                        if (entry.Value != null)
                            reverse[entry.Value] = entry.Key?.ToString() ?? "";
                    }
                }

                string? StateIdOf(object? obj)
                {
                    if (obj == null) return null;
                    if (reverse.TryGetValue(obj, out var id) && !string.IsNullOrWhiteSpace(id))
                        return id;
                    return AnyMember(obj, "Id")?.ToString() ?? AnyMember(obj, "StateId")?.ToString();
                }

                static int? ObjectIdOf(object? obj) => obj == null ? null : RuntimeHelpers.GetHashCode(obj);

                static IEnumerable<FieldInfo> GetFieldsAcrossHierarchy(Type? type)
                {
                    for (var current = type; current != null; current = current.BaseType)
                    {
                        foreach (var field in current.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                            yield return field;
                    }
                }

                var statesList = new List<object?>();
                if (states != null)
                {
                    foreach (System.Collections.DictionaryEntry entry in states)
                    {
                        var st = entry.Value;
                        var stType = st?.GetType().FullName ?? "";
                        var stateInfo = new Dictionary<string, object?>
                        {
                            ["id"] = entry.Key?.ToString(),
                            ["type"] = st?.GetType().Name,
                            ["object_id"] = ObjectIdOf(st),
                            ["internal_fields"] = GetFieldsAcrossHierarchy(st?.GetType())
                                .Where(f => f.Name.Contains("state", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("move", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("count", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("repeat", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("cooldown", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("perform", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("index", StringComparison.OrdinalIgnoreCase))
                                .Select(f => (object?)new Dictionary<string, object?>
                                {
                                    ["name"] = f.Name,
                                    ["type"] = f.FieldType.FullName,
                                        ["value"] = f.GetValue(st)?.ToString(),
                                    })
                                .ToList(),
                            ["all_fields"] = GetFieldsAcrossHierarchy(st?.GetType())
                                .Select(f =>
                                {
                                    object? raw = null;
                                    try { raw = f.GetValue(st); } catch { }
                                    string? value;
                                    if (raw == null) value = null;
                                    else if (raw is string || raw.GetType().IsPrimitive || raw.GetType().IsEnum || raw is decimal)
                                        value = raw.ToString();
                                    else
                                        value = raw.GetType().FullName + ":" + raw;
                                    return (object?)new Dictionary<string, object?>
                                    {
                                        ["name"] = f.Name,
                                        ["type"] = f.FieldType.FullName,
                                        ["value"] = value,
                                    };
                                })
                                .ToList(),
                            ["all_properties"] = st?.GetType()
                                .GetProperties(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                                .Where(p => p.GetIndexParameters().Length == 0)
                                .Select(p =>
                                {
                                    object? raw = null;
                                    try { raw = p.GetValue(st); } catch { }
                                    string? value;
                                    if (raw == null) value = null;
                                    else if (raw is string || raw.GetType().IsPrimitive || raw.GetType().IsEnum || raw is decimal)
                                        value = raw.ToString();
                                    else
                                        value = raw.GetType().FullName + ":" + raw;
                                    return (object?)new Dictionary<string, object?>
                                    {
                                        ["name"] = p.Name,
                                        ["type"] = p.PropertyType.FullName,
                                        ["value"] = value,
                                    };
                                })
                                .ToList(),
                        };

                        if (stType.Contains("RandomBranchState"))
                        {
                            var branches = st?.GetType()
                                .GetField("<States>k__BackingField", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                                ?.GetValue(st) as System.Collections.IEnumerable;
                            stateInfo["branch_count"] = (st?.GetType()
                                .GetField("<States>k__BackingField", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                                ?.GetValue(st) as System.Collections.ICollection)?.Count;
                            if (branches != null)
                            {
                                stateInfo["branches"] = branches.Cast<object?>().Select(b => new Dictionary<string, object?>
                                {
                                    ["object_id"] = ObjectIdOf(b),
                                    ["state_id"] = AnyMember(AnyMember(b, "State"), "StateId")?.ToString()
                                        ?? AnyMember(AnyMember(b, "State"), "Id")?.ToString()
                                        ?? AnyMember(b, "State")?.ToString()
                                        ?? AnyMember(b, "stateId")?.ToString(),
                                    ["state_object_id"] = ObjectIdOf(AnyMember(b, "State")),
                                    ["cooldown"] = AnyMember(b, "Cooldown") ?? AnyMember(b, "cooldown"),
                                    ["max_repeats"] = AnyMember(b, "MaxRepeats") ?? AnyMember(b, "maxRepeats"),
                                    ["repeat_type"] = AnyMember(b, "RepeatType")?.ToString() ?? AnyMember(b, "repeatType")?.ToString(),
                                    ["weight"] = SafeEvaluateBranchWeight(st, b, enemy),
                                    ["internal_fields"] = GetFieldsAcrossHierarchy(b?.GetType())
                                        .Where(f => f.Name.Contains("state", StringComparison.OrdinalIgnoreCase) ||
                                                    f.Name.Contains("repeat", StringComparison.OrdinalIgnoreCase) ||
                                                    f.Name.Contains("cooldown", StringComparison.OrdinalIgnoreCase) ||
                                                    f.Name.Contains("weight", StringComparison.OrdinalIgnoreCase) ||
                                                    f.Name.Contains("count", StringComparison.OrdinalIgnoreCase))
                                        .Select(f => (object?)new Dictionary<string, object?>
                                        {
                                            ["name"] = f.Name,
                                            ["type"] = f.FieldType.FullName,
                                            ["value"] = f.GetValue(b)?.ToString(),
                                        })
                                        .ToList(),
                                    ["weight_lambda_target_fields"] = (AnyMember(b, "weightLambda") as Delegate)?.Target?.GetType()
                                        .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                                        .Select(f =>
                                        {
                                            object? raw = null;
                                            try { raw = f.GetValue((AnyMember(b, "weightLambda") as Delegate)!.Target); } catch { }
                                            return (object?)new Dictionary<string, object?>
                                            {
                                                ["name"] = f.Name,
                                                ["type"] = f.FieldType.FullName,
                                                ["value"] = raw?.ToString(),
                                            };
                                        })
                                        .ToList(),
                                }).Cast<object?>().ToList();
                            }
                        }
                        else if (stType.Contains("ConditionalBranchState"))
                        {
                            var branches = AnyMember(st, "States") as System.Collections.IEnumerable;
                            if (branches != null)
                            {
                                stateInfo["branches"] = branches.Cast<object?>().Select(b => new Dictionary<string, object?>
                                {
                                    ["state_id"] = AnyMember(b, "Move")?.ToString() ?? AnyMember(AnyMember(b, "Move"), "Id")?.ToString() ?? AnyMember(AnyMember(b, "State"), "Id")?.ToString(),
                                    ["condition"] = AnyMember(b, "Condition")?.ToString(),
                                }).Cast<object?>().ToList();
                            }
                        }
                        else if (stType.EndsWith(".MoveState", StringComparison.Ordinal) ||
                                 string.Equals(st?.GetType().Name, "MoveState", StringComparison.Ordinal))
                        {
                            stateInfo["follow_up_state_id"] = AnyMember(st, "FollowUpStateId")?.ToString();
                            stateInfo["must_perform_once_before_transitioning"] = AnyMember(st, "MustPerformOnceBeforeTransitioning");
                            var onPerform = st?.GetType().GetField("_onPerform", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(st) as Delegate;
                            if (onPerform != null)
                            {
                                stateInfo["on_perform_target_fields"] = onPerform.Target?.GetType()
                                    .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                                    .Select(f =>
                                    {
                                        object? raw = null;
                                        try { raw = f.GetValue(onPerform.Target); } catch { }
                                        return (object?)new Dictionary<string, object?>
                                        {
                                            ["name"] = f.Name,
                                            ["type"] = f.FieldType.FullName,
                                            ["value"] = raw?.ToString(),
                                        };
                                    })
                                    .ToList();
                            }
                            var intents = AnyMember(st, "Intents") as System.Collections.IEnumerable;
                            if (intents != null)
                            {
                                stateInfo["intents"] = intents.Cast<object?>().Select(i => new Dictionary<string, object?>
                                {
                                    ["intent_type"] = AnyMember(i, "IntentType")?.ToString(),
                                }).Cast<object?>().ToList();
                            }
                        }

                        statesList.Add(stateInfo);
                    }
                }

                var stateLogDetails = new List<object?>();
                if (stateLog != null)
                {
                    foreach (var item in stateLog)
                    {
                        stateLogDetails.Add(new Dictionary<string, object?>
                        {
                            ["resolved_id"] = StateIdOf(item),
                            ["type"] = item?.GetType().FullName,
                            ["string"] = item?.ToString(),
                            ["members"] = item?.GetType()
                                .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                                .Where(f => f.Name.Contains("id", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("state", StringComparison.OrdinalIgnoreCase) ||
                                            f.Name.Contains("move", StringComparison.OrdinalIgnoreCase))
                                .Select(f => new Dictionary<string, object?>
                                {
                                    ["name"] = f.Name,
                                    ["type"] = f.FieldType.FullName,
                                    ["value"] = f.GetValue(item)?.ToString(),
                                })
                                .ToList(),
                        });
                    }
                }

                List<object?> DescribeFields(object? obj, bool filtered = true) =>
                    GetFieldsAcrossHierarchy(obj?.GetType())
                        .Where(f => !filtered ||
                                    f.Name.Contains("id", StringComparison.OrdinalIgnoreCase) ||
                                    f.Name.Contains("state", StringComparison.OrdinalIgnoreCase) ||
                                    f.Name.Contains("move", StringComparison.OrdinalIgnoreCase) ||
                                    f.Name.Contains("turn", StringComparison.OrdinalIgnoreCase) ||
                                    f.Name.Contains("count", StringComparison.OrdinalIgnoreCase) ||
                                    f.Name.Contains("index", StringComparison.OrdinalIgnoreCase))
                        .Select(f =>
                        {
                            object? raw = null;
                            try { raw = f.GetValue(obj); } catch { }
                            string? value;
                            if (raw == null) value = null;
                            else if (raw is string || raw.GetType().IsPrimitive || raw.GetType().IsEnum || raw is decimal)
                                value = raw.ToString();
                            else
                                value = raw.GetType().FullName + ":" + raw;
                            return (object?)new Dictionary<string, object?>
                            {
                                ["name"] = f.Name,
                                ["type"] = f.FieldType.FullName,
                                ["value"] = value,
                            };
                        })
                        .ToList() ?? new List<object?>();

                result.Add(new Dictionary<string, object?>
                {
                    ["monster_id"] = monster?.Id.Entry,
                    ["monster_object_id"] = ObjectIdOf(monster),
                    ["next_move_id"] = monster?.NextMove?.Id,
                    ["current_state_id"] = AnyMember(currentState, "Id")?.ToString() ?? AnyMember(currentState, "StateId")?.ToString() ?? currentState?.ToString(),
                    ["current_state_object_id"] = ObjectIdOf(currentState),
                    ["initial_state_id"] = AnyMember(initialState, "Id")?.ToString() ?? AnyMember(initialState, "StateId")?.ToString() ?? initialState?.ToString(),
                    ["initial_state_object_id"] = ObjectIdOf(initialState),
                    ["state_machine_object_id"] = ObjectIdOf(sm),
                    ["state_log"] = stateLog?.Cast<object?>().Select(x => x?.ToString()).ToList(),
                    ["state_log_details"] = stateLogDetails,
                    ["monster_internal_fields"] = DescribeFields(monster),
                    ["monster_all_fields"] = DescribeFields(monster, filtered: false),
                    ["state_machine_internal_fields"] = DescribeFields(sm),
                    ["state_machine_all_fields"] = DescribeFields(sm, filtered: false),
                    ["states"] = statesList,
                });
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_enemy_ai_result",
                ["success"] = true,
                ["enemies"] = result,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectEnemyAi failed", ex);
        }
    }

    private static (bool Strict, bool Semantic) ParseSnapshotFingerprintMode(string? mode)
    {
        return (mode ?? "all").Trim().ToLowerInvariant() switch
        {
            "none" => (false, false),
            "strict" => (true, false),
            "all" => (true, true),
            _ => throw new ArgumentException($"Unknown snapshot fingerprint mode: {mode}"),
        };
    }

    private static string GetCombatSnapshotFingerprint(CombatSnapshot snapshot)
    {
        return snapshot.CachedStateFingerprint ??=
            ComputeCombatSnapshotFingerprint(snapshot);
    }

    private static string GetCombatSnapshotSemanticFingerprint(CombatSnapshot snapshot)
    {
        return snapshot.CachedSemanticStateFingerprint ??=
            ComputeCombatSnapshotSemanticFingerprint(snapshot);
    }

    private static void AttachCombatSnapshotFingerprints(
        Dictionary<string, object?> result,
        CombatSnapshot snapshot,
        string? fingerprintMode)
    {
        var (strict, semantic) = ParseSnapshotFingerprintMode(fingerprintMode);
        result["fingerprint_mode"] = semantic ? "all" : strict ? "strict" : "none";
        if (strict)
        {
            result["state_fingerprint"] = GetCombatSnapshotFingerprint(snapshot);
            result["state_fingerprint_schema"] = "sts2-combat-snapshot-v1";
        }
        if (semantic)
        {
            result["semantic_state_fingerprint"] =
                GetCombatSnapshotSemanticFingerprint(snapshot);
            result["semantic_state_fingerprint_schema"] = "sts2-combat-semantic-v1";
        }
    }

    public Dictionary<string, object?> CaptureCombatSnapshot(
        string? snapshotId = null,
        string fingerprintMode = "all")
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");
            if (!CombatManager.Instance.IsInProgress)
                return Error("Not in combat");

            var room = _runState.CurrentRoom as CombatRoom;
            var player = _runState.Players.FirstOrDefault();
            var combatState = CombatManager.Instance.DebugOnlyGetState();
            if (room == null || player == null || combatState == null)
                return Error("Combat snapshot unavailable");

            var netState = NetFullCombatState.FromRun(_runState, justFinishedAction: null!);
            if (netState == null)
                return Error("NetFullCombatState.FromRun returned null");

            var id = string.IsNullOrWhiteSpace(snapshotId) ? $"combat_{Guid.NewGuid():N}" : snapshotId!;
            var historyEntryRefs = CaptureCombatHistoryEntryRefs();
            var runtimeCapture = new RuntimeCaptureContext { Player = player, CombatState = combatState };
            var historyEntries = CaptureRuntimeValues(historyEntryRefs, runtimeCapture);
            var activePowerRefs = CaptureActivePowerRefs(runtimeCapture);
            var capturedSnapshot = new CombatSnapshot
            {
                Id = id,
                CharacterName = player.Character?.Id.Entry ?? "IRONCLAD",
                AscensionLevel = _runState.AscensionLevel,
                Seed = GetMember(netState.Rng, "Seed")?.ToString() ?? "restored",
                ActIndex = _runState.CurrentActIndex,
                ActFloor = _runState.ActFloor,
                BossEncounterId = TryCaptureBossEncounterId(),
                Room = CaptureSearchCombatRoom(room),
                Player = player.ToSerializable(),
                NetState = netState,
                RuntimeCardCosts = CaptureRuntimeCardCosts(player),
                EnemyCreatureStates = CaptureEnemyCreatureStates(
                    combatState.Enemies?.Where(e => e != null).ToList() ?? new List<Creature>()),
                EnemyAiStates = CaptureEnemyAiStates(combatState.Enemies?.Where(e => e != null).ToList() ?? new List<Creature>()),
                RunRngStates = CaptureDetailedRngStates(_runState.Rng),
                PlayerRngStates = CaptureDetailedRngStates(player.PlayerRng),
                RoundNumber = combatState.RoundNumber,
                CurrentSide = combatState.CurrentSide,
                RelicStates = CaptureRelicStates(player),
                HookStates = CaptureHookStates(combatState),
                PlayerCombatState = CapturePrimitiveObjectState(player.PlayerCombatState),
                PlayerExtraState = CapturePrimitiveObjectState(player.ExtraFields),
                CombatHistoryEntries = historyEntries,
                ActivePowerRefs = activePowerRefs,
                CombatHistoryEntryRefs = historyEntryRefs,
            };
            _combatSnapshots[id] = capturedSnapshot;

            var result = new Dictionary<string, object?>
            {
                ["type"] = "combat_snapshot_captured",
                ["success"] = true,
                ["snapshot_id"] = id,
                ["round_number"] = combatState.RoundNumber,
                ["current_side"] = combatState.CurrentSide.ToString(),
                ["encounter_id"] = room.Encounter?.Id?.Entry,
                ["player_hp"] = player.Creature?.CurrentHp,
                ["enemy_count"] = combatState.Enemies?.Count ?? 0,
                ["run_rng_streams"] = capturedSnapshot.RunRngStates.Count,
                ["run_rng_complete_streams"] = capturedSnapshot.RunRngStates.Count(state =>
                    state.S0.HasValue && state.S1.HasValue && state.S2.HasValue && state.S3.HasValue),
                ["player_rng_streams"] = capturedSnapshot.PlayerRngStates.Count,
                ["player_rng_complete_streams"] = capturedSnapshot.PlayerRngStates.Count(state =>
                    state.S0.HasValue && state.S1.HasValue && state.S2.HasValue && state.S3.HasValue),
            };
            AttachCombatSnapshotFingerprints(result, capturedSnapshot, fingerprintMode);
            return result;
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("CaptureCombatSnapshot failed", ex);
        }
    }

    private static SerializableRoom CaptureSearchCombatRoom(CombatRoom room)
    {
        if (room.ParentEventId == null || room.IsPreFinished)
            return room.ToSerializable();

        // Native disk saves reject active event combats. Search snapshots also
        // carry the live combat state, so preserve the native room payload here
        // without completing or detaching the source event (CombatRoom.ToSerializable).
        var saved = new SerializableRoom
        {
            RoomType = room.RoomType,
            EncounterId = room.Encounter.Id,
            IsPreFinished = room.IsPreFinished,
            GoldProportion = room.GoldProportion,
            ParentEventId = room.ParentEventId,
            ShouldResumeParentEvent = room.ShouldResumeParentEventAfterCombat,
            EncounterState = room.Encounter.SaveCustomState(),
        };
        foreach (var (player, rewards) in room.ExtraRewards)
            saved.ExtraRewards[player.NetId] = rewards.Select(reward => reward.ToSerializable()).ToList();
        return saved;
    }

    public Dictionary<string, object?> FingerprintCombatSnapshot(
        string snapshotId,
        string fingerprintMode = "all")
    {
        try
        {
            if (!_combatSnapshots.TryGetValue(snapshotId, out var snapshot))
                return Error($"Unknown combat snapshot: {snapshotId}");
            var result = new Dictionary<string, object?>
            {
                ["type"] = "combat_snapshot_fingerprinted",
                ["success"] = true,
                ["snapshot_id"] = snapshotId,
            };
            AttachCombatSnapshotFingerprints(result, snapshot, fingerprintMode);
            return result;
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("FingerprintCombatSnapshot failed", ex);
        }
    }

    public Dictionary<string, object?> ExpandCombatChildren(
        string parentSnapshotId,
        IReadOnlyList<CombatChildExpansionRequest> children,
        string lang = "en",
        string fingerprintMode = "all")
    {
        var batchStarted = timeMs();
        var rows = new List<Dictionary<string, object?>>(children.Count);
        for (var index = 0; index < children.Count; index++)
        {
            var request = children[index];
            var rowStarted = timeMs();
            var row = new Dictionary<string, object?>
            {
                ["request_index"] = index,
                ["action"] = request.Action,
                ["snapshot_id"] = request.SnapshotId,
            };
            try
            {
                var restoreResult = RestoreCombatSnapshot(
                    parentSnapshotId, lang, allowFull: true, forceFull: false);
                row["restore_result"] = CompactBatchRestoreResult(restoreResult);
                if (restoreResult.TryGetValue("type", out var restoreType) &&
                    string.Equals(restoreType?.ToString(), "error", StringComparison.Ordinal))
                {
                    row["success"] = false;
                    row["message"] = "Parent snapshot restore failed";
                    rows.Add(row);
                    continue;
                }

                var actionStarted = System.Diagnostics.Stopwatch.GetTimestamp();
                BeginActionExecutionProfile();
                var actionResult = ExecuteAction(request.Action, request.Args);
                AttachActionExecutionProfile(actionResult);
                actionResult["headless_execute_ms"] =
                    System.Diagnostics.Stopwatch.GetElapsedTime(actionStarted).TotalMilliseconds;

                var isError = actionResult.TryGetValue("type", out var actionType) &&
                    string.Equals(actionType?.ToString(), "error", StringComparison.Ordinal);
                var isCombatPlay = actionResult.TryGetValue("decision", out var decision) &&
                    string.Equals(decision?.ToString(), "combat_play", StringComparison.Ordinal);
                row["action_result"] = isCombatPlay
                    ? CompactBatchActionResult(actionResult)
                    : actionResult;

                if (isError)
                {
                    row["success"] = false;
                    row["message"] = actionResult.GetValueOrDefault("message");
                    rows.Add(row);
                    continue;
                }

                if (!isCombatPlay || !CombatManager.Instance.IsInProgress)
                {
                    row["success"] = true;
                    row["terminal"] = true;
                    rows.Add(row);
                    continue;
                }

                var captureStarted = timeMs();
                var captureResult = CaptureCombatSnapshot(
                    request.SnapshotId, fingerprintMode);
                var captureMs = timeMs() - captureStarted;
                captureResult["headless_capture_ms"] = captureMs;
                row["capture_ms"] = captureMs;
                row["snapshot_result"] = captureResult;
                if (!captureResult.TryGetValue("success", out var captureSuccess) ||
                    captureSuccess is not bool captureOk || !captureOk)
                {
                    row["success"] = false;
                    row["message"] = "Could not capture child snapshot";
                    rows.Add(row);
                    continue;
                }

                // Match the ordinary replay path exactly: capture the reusable
                // child checkpoint first, then build the search projection from
                // that post-capture engine state. NetFullCombatState extraction
                // during capture can initialize runtime identifiers, so reading
                // the projection before capture creates a different observation
                // boundary even when the action itself is identical.
                var stateStarted = timeMs();
                var stateResult = GetCurrentSearchState();
                var stateMs = timeMs() - stateStarted;
                stateResult["headless_build_state_ms"] = stateMs;
                row["state_ms"] = stateMs;
                if (!stateResult.TryGetValue("combat_state_for_search", out var stateObj) ||
                    stateObj is not Dictionary<string, object?> searchState ||
                    !searchState.TryGetValue("success", out var stateSuccess) ||
                    stateSuccess is not bool stateOk || !stateOk)
                {
                    row["success"] = false;
                    row["search_state_result"] = stateResult;
                    row["message"] = "Could not build child search state";
                    rows.Add(row);
                    continue;
                }

                var fingerprint = captureResult.GetValueOrDefault("state_fingerprint")?.ToString();
                var semanticFingerprint = captureResult
                    .GetValueOrDefault("semantic_state_fingerprint")?.ToString();
                searchState["engine_snapshot_id"] = request.SnapshotId;
                if (!string.IsNullOrWhiteSpace(fingerprint))
                {
                    searchState["engine_state_fingerprint"] = fingerprint;
                    searchState["engine_state_fingerprint_schema"] = "sts2-combat-snapshot-v1";
                    row["state_fingerprint"] = fingerprint;
                }
                if (!string.IsNullOrWhiteSpace(semanticFingerprint))
                {
                    searchState["engine_semantic_state_fingerprint"] = semanticFingerprint;
                    searchState["engine_semantic_state_fingerprint_schema"] =
                        "sts2-combat-semantic-v1";
                    row["semantic_state_fingerprint"] = semanticFingerprint;
                }
                row["combat_state_for_search"] = searchState;
                row["success"] = true;
                row["terminal"] = false;
            }
            catch (Exception ex)
            {
                row["success"] = false;
                row["type"] = "error";
                row["message"] = ex.Message;
                row["trace"] = ex.ToString();
            }
            finally
            {
                row["elapsed_ms"] = timeMs() - rowStarted;
            }
            rows.Add(row);
        }

        return new Dictionary<string, object?>
        {
            ["type"] = "combat_children_expanded",
            ["success"] = true,
            ["parent_snapshot_id"] = parentSnapshotId,
            ["fingerprint_mode"] = fingerprintMode,
            ["children"] = rows,
            ["elapsed_ms"] = timeMs() - batchStarted,
        };
    }

    public Dictionary<string, object?> RestoreCombatSnapshot(
        string snapshotId,
        string lang = "en",
        bool allowFull = true,
        bool forceFull = false)
    {
        var timing = new Dictionary<string, object?>();
        try
        {
            var restoreStarted = timeMs();
            _loc.Lang = lang;
            EnsureModelDbInitialized();
            timing["init_ms"] = timeMs() - restoreStarted;

            if (!_combatSnapshots.TryGetValue(snapshotId, out var snapshot))
                return Error($"Unknown combat snapshot: {snapshotId}");

            var attemptStarted = timeMs();
            // STS2_FORCE_FULL_RESTORE=1 disables the in_place fast path entirely.
            // in_place repairs a reused worker's live combat to match the
            // snapshot, but a worker churned across many search candidates can
            // accumulate engine state corruption (torn-down combat phase, stale
            // enemy/turn state) that in_place silently fails to fully reset —
            // producing phantom card_reward "victories" on lethal end_turns and
            // empty-enemy search states. full restore tears down and rebuilds the
            // combat from scratch, so it is immune. This flag trades the ~6x pool
            // speedup for guaranteed correctness; use it to validate parity and
            // to produce trustworthy learning/baseline data.
            forceFull = forceFull || Environment.GetEnvironmentVariable("STS2_FORCE_FULL_RESTORE") == "1";
            string inPlaceFailReason = "forced_full";
            if (!forceFull && TryRestoreCombatSnapshotInPlace(snapshot, out var fastResult, out inPlaceFailReason))
            {
                fastResult["restored_snapshot_id"] = snapshotId;
                fastResult["restore_mode"] = "in_place";
                timing["in_place_attempt_ms"] = timeMs() - attemptStarted;
                timing["total_ms"] = timeMs() - restoreStarted;
                fastResult["restore_timing_ms"] = timing;
                return fastResult;
            }
            timing["in_place_attempt_ms"] = timeMs() - attemptStarted;
            // Diagnostic: surface WHY the fast path was rejected so callers can
            // tally how many full (slow) restores were avoidable. Pure
            // observability; does not change control flow.
            timing["in_place_fail_reason"] = inPlaceFailReason;

            // STS2_STRICT_RESTORE=1: data-collection sentinel. When the in_place
            // fast path was rejected by the SANITY check (not by a benign
            // precondition like no_run_in_progress / encounter_mismatch, which
            // are expected and correctly handled by full restore), the live
            // worker state diverged from the snapshot in a way the integrity
            // check could see. Under strict mode we surface that as an explicit
            // error instead of silently masking it with a full restore, so the
            // data layer can mark the episode dirty and the divergence gets
            // investigated rather than averaged into "clean" training data.
            var strict = Environment.GetEnvironmentVariable("STS2_STRICT_RESTORE") == "1";
            if (strict && inPlaceFailReason.StartsWith("sanity_mismatch:", StringComparison.Ordinal))
            {
                timing["total_ms"] = timeMs() - restoreStarted;
                var strictErr = Error($"STRICT restore: in_place sanity mismatch ({inPlaceFailReason})");
                strictErr["restored_snapshot_id"] = snapshotId;
                strictErr["restore_mode"] = "strict_reject";
                strictErr["in_place_fail_reason"] = inPlaceFailReason;
                strictErr["restore_timing_ms"] = timing;
                return strictErr;
            }

            if (!allowFull)
            {
                timing["total_ms"] = timeMs() - restoreStarted;
                var rejected = Error($"In-place combat snapshot restore failed and full restore is disabled: {inPlaceFailReason}");
                rejected["restored_snapshot_id"] = snapshotId;
                rejected["restore_mode"] = "none";
                rejected["restore_timing_ms"] = timing;
                return rejected;
            }

            var fullStarted = timeMs();
            var result = RestoreCombatSnapshotFull(snapshot, timing);
            timing["full_restore_ms"] = timeMs() - fullStarted;
            timing["total_ms"] = timeMs() - restoreStarted;
            result["restored_snapshot_id"] = snapshotId;
            result["restore_mode"] = "full";
            // Full restore was treated as the "immune" path and never self-checked.
            // It IS immune to worker-churn corruption (it tears down and rebuilds),
            // but a full restore that still fails to reproduce the snapshot points
            // at a genuine ENGINE bug (a field the apply path does not restore),
            // not transient churn — exactly the class of silent corruption that
            // poisoned past learning data. Validate it; attach a visible warning
            // (and, under strict mode, hard-fail) so such a bug can never pass
            // unnoticed again.
            if (_runState?.CurrentRoom is CombatRoom fullRoom)
            {
                var fullPlayer = _runState.Players.FirstOrDefault();
                if (fullPlayer != null && !RestoreMatchesSnapshotSanity(snapshot, fullRoom, fullPlayer))
                {
                    var detail = _lastSanityFailDetail;
                    result["restore_sanity_warning"] = $"full_restore_sanity_mismatch:{detail}";
                    Log($"WARNING: full restore sanity mismatch: {detail}");
                    if (strict)
                    {
                        timing["total_ms"] = timeMs() - restoreStarted;
                        var fullErr = Error($"STRICT restore: full restore sanity mismatch ({detail})");
                        fullErr["restored_snapshot_id"] = snapshotId;
                        fullErr["restore_mode"] = "strict_reject_full";
                        fullErr["restore_sanity_detail"] = detail;
                        fullErr["restore_timing_ms"] = timing;
                        return fullErr;
                    }
                }
            }
            result["restore_timing_ms"] = timing;
            return result;
        }
        catch (Exception ex)
        {
            var error = ErrorWithTrace("RestoreCombatSnapshot failed", ex);
            error["membership_debug"] = TryInspectCombatMembershipForError();
            return error;
        }
    }

    private bool TryRestoreCombatSnapshotInPlace(CombatSnapshot snapshot, out Dictionary<string, object?> result, out string failReason)
    {
        result = new Dictionary<string, object?>();
        failReason = "";
        try
        {
            if (_runState == null || !RunManager.Instance.IsInProgress)
            {
                failReason = "no_run_in_progress";
                return false;
            }
            if (_runState.CurrentRoom is not CombatRoom room)
            {
                failReason = "current_room_not_combat";
                return false;
            }
            // NOTE: a churned worker may have IsInProgress==false here (prior
            // replayed line ended combat). We no longer bail to full restore for
            // that — ApplyCapturedCombatSnapshot repairs the data and
            // ReactivateCombatPhaseIfNeeded (below, after apply) re-arms the
            // phase, keeping the fast in_place path correct.

            var player = _runState.Players.FirstOrDefault();
            if (player?.PlayerCombatState == null || player.Creature == null)
            {
                failReason = "player_combat_state_null";
                return false;
            }

            // Precondition: in_place restore only repairs a live combat that is
            // the SAME encounter as the snapshot. The reconcile/apply step mutates
            // the live enemy list to match the snapshot, so the post-hoc sanity
            // check below cannot detect a cross-encounter restore — by then the
            // list has already been forced to match. A worker reused across
            // DIFFERENT combats (e.g. NIBBITS -> SHRINKER_BEETLE) would otherwise
            // be "restored" in place onto an incompatible live combat, silently
            // producing a corrupt (all-null) search state that reports success.
            // When the live encounter differs from the snapshot, reject so the
            // caller falls back to a full restore (still warm: no ~241ms ModelDB
            // init, only the run bootstrap). Within-combat search restores are
            // same-encounter and keep the fast in_place path.
            if (!LiveCombatMatchesSnapshotEncounter(room, snapshot, out var encounterMismatch))
            {
                failReason = $"encounter_mismatch:{encounterMismatch}";
                return false;
            }

            ResetTransientRunStateForCombatRestore(player);
            ApplyCapturedCombatSnapshot(snapshot, room, player, useCapturedMoveCallbacks: true);
            ApplyCapturedRunContext(snapshot);
            _syncCtx.Pump();
            WaitForActionExecutor();

            // Phase reactivation (root-caused 2026-06-14 via decompiled CombatManager):
            // a worker reused across search candidates can be left with combat
            // DATA intact (_state has live enemies, correct HP — ApplyCapturedCombatSnapshot
            // repaired it) but the combat PHASE torn down: CombatManager.IsInProgress
            // == false, because a prior replayed line ran EndCombatInternal/LoseCombat
            // (IsInProgress=false at decompiled lines 722/761/772). The search-state
            // export then reads "not combat_play" and emits empty enemies/actions,
            // which the searcher misreads as a phantom victory on lethal end_turns.
            // Only StartCombatInternal flips IsInProgress back to true, and in_place
            // never calls it. Reactivate the phase directly (the headless-relevant
            // subset of StartCombatInternal lines 319-321 / 489-491), so in_place
            // produces a state byte-equivalent to a clean restore — without the 225x
            // full-restore penalty. 100/102 field diffs vs a clean restore were this
            // single phase-failure cascade (measured).
            ReactivateCombatPhaseIfNeeded();

            result = FinalizeRestoredCombatSnapshot(snapshot, room, player);
            if (!RestoreMatchesSnapshotSanity(snapshot, room, player))
            {
                failReason = $"sanity_mismatch:{_lastSanityFailDetail}";
                return false;
            }
            return true;
        }
        catch (Exception ex)
        {
            Log($"In-place restore fallback: {ex.GetType().Name}: {ex.Message}");
            result = new Dictionary<string, object?>();
            failReason = $"exception:{ex.GetType().Name}";
            return false;
        }
    }

    // Re-arm the combat phase after an in_place restore onto a worker whose
    // prior churn ended combat (IsInProgress=false) while leaving combat data
    // intact. Mirrors the phase-relevant subset of StartCombatInternal /
    // turn-start (decompiled CombatManager): set IsInProgress + IsPlayPhase via
    // their compiler-generated backing fields (private setters), and drive the
    // action executor + synchronizer back into the play phase. No-op when combat
    // is already in progress (the normal case), so it costs nothing on the hot
    // path and only repairs the churned-zombie case.
    private static bool IsPlayPhase()
    {
        var combatState = CombatManager.Instance?.DebugOnlyGetState();
        if (combatState == null)
            return false;

        try
        {
            return LocalContext.GetMe(combatState).PlayerCombatState.Phase == PlayerTurnPhase.Play;
        }
        catch
        {
            return combatState.Players.FirstOrDefault()?.PlayerCombatState.Phase == PlayerTurnPhase.Play;
        }
    }

    private void ReactivateCombatPhaseIfNeeded()
    {
        try
        {
            var cm = CombatManager.Instance;
            if (cm == null || cm.DebugOnlyGetState() == null)
                return; // no live combat data to re-arm; leave for full restore

            // Re-arm the combat phase whenever it is not already in the play
            // phase. Two churn shapes reach here:
            //   (1) !IsInProgress  — the run/combat phase was fully torn down by a
            //       prior replayed terminal line (game_over/victory).
            //   (2) IsInProgress && !IsPlayPhase — combat is live but stuck in a
            //       non-play phase. This is the turn-root case: search snapshots
            //       for multi-enemy imported roots are captured ONLY after
            //       end_turn (_is_safe_snapshot_checkpoint), i.e. at the next
            //       player decision point AFTER the enemy turn resolved. The
            //       capture is a valid play-phase state, but in_place restore does
            //       not re-establish IsPlayPhase, so a later sibling that restores
            //       this intermediate snapshot reads IsPlayPhase=false ->
            //       BuildLiveCombatSearchState errors "not combat_play" -> the
            //       searcher saw an empty/error leaf (observed seed42 THE_KIN s14:
            //       231 errors, all IsInProgress=True IsPlayPhase=False
            //       aliveEnemies=3 playerDead=False). Full restore avoids this by
            //       driving EnterRoom; in_place must re-arm the flag itself.
            // Both shapes want the same repair: make this a live player play-phase.
            if (!cm.IsInProgress || !IsPlayPhase())
            {
                SetField(cm, "<IsInProgress>k__BackingField", true);
                foreach (var player in cm.DebugOnlyGetState()!.Players)
                    player.PlayerCombatState.Phase = PlayerTurnPhase.Play;

                try
                {
                    RunManager.Instance.ActionQueueSynchronizer?.SetCombatState(
                        ActionSynchronizerCombatState.PlayPhase);
                }
                catch (Exception ex) { Log($"ReactivatePhase synchronizer: {ex.Message}"); }
                try
                {
                    RunManager.Instance.ActionExecutor?.Unpause();
                }
                catch (Exception ex) { Log($"ReactivatePhase executor: {ex.Message}"); }
            }

            // Clear the per-turn ready/extra-turn sets. These are CombatManager
            // instance HashSets that StartTurn() clears at the start of every
            // turn, but in_place restore does not touch them. A churned worker can
            // carry a stale player in _playersReadyToEndTurn from a prior replayed
            // line; SetReadyToEndTurn then early-returns ("already ready") and the
            // enemy turn never fires — the lethal end_turn resolves to the
            // unchanged pre-enemy-turn state (phantom survival). Resetting them is
            // what StartTurn would have done; it makes end_turn drive the enemy
            // turn correctly on a reused worker.
            ClearPlayerReadySets(cm);

            // Reset Player.IsActiveForHooks. It is set to Creature.IsAlive only at
            // construction/FromSerializable and flipped false mid-combat (e.g. on
            // death), but in_place restore never resets it. A churned worker whose
            // prior replayed line drove the player to death kept
            // IsActiveForHooks=false; then IterateHookListeners skips ALL player
            // relics/potions/cards (CombatState line "if (!player.IsActiveForHooks)
            // continue"), so player-side damage modifiers drop out and the enemy's
            // intent damage is computed too high (observed 12 -> 24, doubling a
            // lethal hit) — turning a survivable line into a phantom death. Restore
            // it to the live creature's alive state, as construction would.
            try
            {
                foreach (var p in _runState.Players)
                {
                    if (p?.Creature != null)
                        SetField(p, "<IsActiveForHooks>k__BackingField", p.Creature.IsAlive);
                }
            }
            catch (Exception ex) { Log($"ReactivatePhase IsActiveForHooks: {ex.Message}"); }

            _syncCtx.Pump();
            WaitForActionExecutor();
        }
        catch (Exception ex)
        {
            Log($"ReactivateCombatPhaseIfNeeded failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private static void ClearPlayerReadySets(CombatManager cm)
    {
        foreach (var fieldName in new[] { "_playersReadyToEndTurn", "_playersReadyToBeginEnemyTurn", "_playersTakingExtraTurn" })
        {
            try
            {
                var f = typeof(CombatManager).GetField(fieldName,
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                var coll = f?.GetValue(cm);
                var clear = coll?.GetType().GetMethod("Clear", System.Type.EmptyTypes);
                clear?.Invoke(coll, null);
            }
            catch { /* best-effort; a missing set just means nothing to clear */ }
        }
    }

    private static object? ReadPrivateInstanceField(object? instance, string fieldName)
    {
        if (instance == null)
            return null;
        for (var type = instance.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetField(fieldName,
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (field != null)
                return field.GetValue(instance);
        }
        return null;
    }

    private static int? CollectionCount(object? collection)
    {
        if (collection == null)
            return null;
        var count = collection.GetType().GetProperty("Count", BindingFlags.Instance | BindingFlags.Public);
        if (count == null)
            return null;
        try { return Convert.ToInt32(count.GetValue(collection)); }
        catch { return null; }
    }

    private Dictionary<string, object?> InspectEndTurnControlState(Player player)
    {
        var cm = CombatManager.Instance;
        var state = cm?.DebugOnlyGetState();
        var synchronizer = RunManager.Instance.ActionQueueSynchronizer;
        var executor = RunManager.Instance.ActionExecutor;
        var readyToEnd = ReadPrivateInstanceField(cm, "_playersReadyToEndTurn");
        var readyToBeginEnemy = ReadPrivateInstanceField(cm, "_playersReadyToBeginEnemyTurn");
        var extraTurns = ReadPrivateInstanceField(cm, "_playersTakingExtraTurn");
        return new Dictionary<string, object?>
        {
            ["is_in_progress"] = cm?.IsInProgress,
            ["is_paused"] = cm?.IsPaused,
            ["is_play_phase"] = IsPlayPhase(),
            ["current_side"] = state?.CurrentSide.ToString(),
            ["player_phase"] = player.PlayerCombatState?.Phase.ToString(),
            ["ending_player_turn_phase_one"] = cm?.EndingPlayerTurnPhaseOne,
            ["ending_player_turn_phase_two"] = cm?.EndingPlayerTurnPhaseTwo,
            ["is_enemy_turn_started"] = cm?.IsEnemyTurnStarted,
            ["in_player_turn_setup"] = ReadPrivateInstanceField(cm, "_inPlayerTurnSetup"),
            ["has_deferred_end_turn_transition"] = ReadPrivateInstanceField(cm, "_deferredEndTurnTransition") != null,
            ["ready_to_end_count"] = CollectionCount(readyToEnd),
            ["ready_to_begin_enemy_count"] = CollectionCount(readyToBeginEnemy),
            ["extra_turn_count"] = CollectionCount(extraTurns),
            ["synchronizer_combat_state"] = synchronizer?.CombatState.ToString(),
            ["action_executor_running"] = executor?.IsRunning,
            ["current_action_type"] = executor?.CurrentlyRunningAction?.GetType().FullName,
            ["state_player_is_command_player"] = state?.Players.Any(p => ReferenceEquals(p, player)),
            ["active_enemy_turn_tasks"] = EnemyTurnTaskTrace.Snapshot(),
        };
    }

    private Dictionary<string, object?> RestoreCombatSnapshotFull(CombatSnapshot snapshot, Dictionary<string, object?> timing)
    {
        var segmentStarted = timeMs();
        if (_runState != null || RunManager.Instance.DebugOnlyGetState() != null || RunManager.Instance.IsInProgress)
            CleanUp(keepProcessAlive: true);
        timing["cleanup_ms"] = timeMs() - segmentStarted;

        segmentStarted = timeMs();
        var player = Player.FromSerializable(snapshot.Player);
        _runState = RunState.CreateForTest(
            players: new[] { player },
            ascensionLevel: snapshot.AscensionLevel,
            seed: snapshot.Seed
        );
        timing["create_run_ms"] = timeMs() - segmentStarted;

        segmentStarted = timeMs();
        var netService = new NetSingleplayerGameService();
        RunManager.Instance.SetUpTest(_runState, netService);
        LocalContext.NetId = netService.NetId;
        ApplyCapturedRunContext(snapshot);
        timing["setup_test_ms"] = timeMs() - segmentStarted;

        segmentStarted = timeMs();
        RegisterCombatEventHandlers();
        EnsureCardSelectorInstalled();
        timing["handler_setup_ms"] = timeMs() - segmentStarted;

        segmentStarted = timeMs();
        var room = CombatRoom.FromSerializable(snapshot.Room, _runState);
        RunManager.Instance.EnterRoom(room).GetAwaiter().GetResult();
        _syncCtx.Pump();
        WaitForActionExecutor();
        timing["enter_room_ms"] = timeMs() - segmentStarted;

        segmentStarted = timeMs();
        ResetTransientRunStateForCombatRestore(player);
        ApplyCapturedCombatSnapshot(snapshot, room, player, useCapturedMoveCallbacks: false);
        ApplyCapturedRunContext(snapshot);
        _syncCtx.Pump();
        WaitForActionExecutor();
        timing["apply_snapshot_ms"] = timeMs() - segmentStarted;

        segmentStarted = timeMs();
        var result = FinalizeRestoredCombatSnapshot(snapshot, room, player);
        timing["finalize_restore_ms"] = timeMs() - segmentStarted;
        return result;
    }

    private Dictionary<string, object?> FinalizeRestoredCombatSnapshot(CombatSnapshot snapshot, CombatRoom room, Player player)
    {
        var result = DetectDecisionPoint();
        // Test-run construction resets reward odds; retain the captured odds
        // alongside RNG so restored victory branches generate the same rewards.
        player.PlayerOdds.LoadFromSerializable(snapshot.Player.Odds);
        var combatState = CombatManager.Instance.DebugOnlyGetState() ?? room.CombatState;
        if (combatState != null)
            ReconcileEnemyListToSnapshot(combatState, snapshot.EnemyCreatureStates);
        ApplyDetailedRngStates(snapshot.RunRngStates, _runState.Rng);
        ApplyRunRngCounters(GetMember(snapshot.NetState, "Rng"), _runState.Rng);
        ApplyDetailedRngStates(snapshot.PlayerRngStates, player.PlayerRng);
        if (GetMember(snapshot.NetState, "Players") is System.Collections.IEnumerable restoredPlayers)
        {
            var restoredPlayerSnapshot = restoredPlayers.Cast<object?>().FirstOrDefault();
            if (restoredPlayerSnapshot != null && GetMember(restoredPlayerSnapshot, "rngSet") is object restoredPlayerRngSnapshot)
                ApplyPlayerRngCounters(restoredPlayerRngSnapshot, player.PlayerRng);
        }
        return result;
    }

    private void ApplyCapturedRunContext(CombatSnapshot snapshot)
    {
        if (_runState == null)
            throw new InvalidOperationException("Cannot restore run context without a run");
        if (snapshot.ActIndex.HasValue)
        {
            var index = snapshot.ActIndex.Value;
            if (index < 0 || index >= _runState.Acts.Count)
                throw new InvalidOperationException($"Invalid captured act index: {index}");
            _runState.CurrentActIndex = index;
        }
        if (snapshot.ActFloor.HasValue)
        {
            if (snapshot.ActFloor.Value < 0)
                throw new InvalidOperationException("Invalid captured act floor");
            _runState.ActFloor = snapshot.ActFloor.Value;
        }
        if (!string.IsNullOrEmpty(snapshot.BossEncounterId))
        {
            var boss = ModelDb.GetById<EncounterModel>(new ModelId("ENCOUNTER", snapshot.BossEncounterId));
            if (boss == null || _runState.Act == null)
                throw new InvalidOperationException($"Unknown captured boss encounter: {snapshot.BossEncounterId}");
            _runState.Act.SetBossEncounter(boss.ToMutable());
        }
    }

    private string? TryCaptureBossEncounterId()
    {
        try { return _runState?.Act?.BossEncounter?.Id?.Entry; }
        catch (InvalidOperationException) { return null; }
    }

    private void ResetTransientRunStateForCombatRestore(Player player)
    {
        EnemyTurnTaskTrace.Reset();
        _combatRewardsSet = null;
        _combatRewardsCompletion = null;
        _pendingCombatRewards.Clear();
        _activeCombatCardReward = null;
        _pendingBundles = null;
        _pendingBundleTcs = null;
        _rewardsProcessed = false;
        _pendingInteractionTask = null;
        _goldBeforeCombat = player.Gold;
        _lastKnownHp = player.Creature?.CurrentHp ?? 0;
        _turnStarted.Reset();
        _combatEnded.Reset();
        _cardSelector.CancelPending();
    }

    private bool RestoreMatchesSnapshotSanity(CombatSnapshot snapshot, CombatRoom room, Player player)
    {
        _lastSanityFailDetail = "";
        try
        {
            var activeState = CombatManager.Instance.DebugOnlyGetState();
            if (activeState == null || player.PlayerCombatState == null || player.Creature == null)
            {
                _lastSanityFailDetail = "null_state";
                return false;
            }

            // Result-oriented self-check: the SEARCH-STATE export the searcher
            // actually consumes reads CombatManager.Instance.DebugOnlyGetState()
            // (see BuildCombatStateForSearch), NOT room.CombatState. A churned
            // worker can leave these two decoupled — room.CombatState is repaired
            // correctly (so the room-based checks below pass) while the active
            // CombatManager state the searcher reads has an empty/dead enemy
            // list. That gap is the phantom-victory root cause. Validate the
            // CONSUMED source directly: its living-enemy count must match the
            // snapshot. If it does not, in_place produced a state the searcher
            // would misread, so reject and fall back to full restore.
            var activeAlive = (activeState?.Enemies ?? Enumerable.Empty<Creature>())
                .Count(e => e != null && e.IsAlive);
            var expectedAlive = snapshot.EnemyCreatureStates.Count(enemy => enemy.CurrentHp > 0);
            if (activeAlive != expectedAlive)
            {
                _lastSanityFailDetail =
                    $"active_enemy_count:{activeAlive}!={expectedAlive}";
                return false;
            }

            if (activeState.RoundNumber != snapshot.RoundNumber)
            {
                _lastSanityFailDetail = $"round:{activeState.RoundNumber}!={snapshot.RoundNumber}";
                return false;
            }
            if (activeState.CurrentSide != snapshot.CurrentSide)
            {
                _lastSanityFailDetail = $"side:{activeState.CurrentSide}!={snapshot.CurrentSide}";
                return false;
            }

            var liveEnemies = activeState.Enemies?.Where(e => e != null).ToList() ?? new List<Creature>();
            if (liveEnemies.Count != snapshot.EnemyCreatureStates.Count)
            {
                _lastSanityFailDetail = $"enemy_count:{liveEnemies.Count}!={snapshot.EnemyCreatureStates.Count}";
                return false;
            }

            var aiQueues = new Dictionary<string, Queue<CombatSnapshot.EnemyAiSnapshot>>(StringComparer.Ordinal);
            foreach (var aiSnapshot in snapshot.EnemyAiStates)
            {
                if (!aiQueues.TryGetValue(aiSnapshot.MonsterId, out var queue))
                {
                    queue = new Queue<CombatSnapshot.EnemyAiSnapshot>();
                    aiQueues[aiSnapshot.MonsterId] = queue;
                }
                queue.Enqueue(aiSnapshot);
            }

            for (var i = 0; i < liveEnemies.Count; i++)
            {
                var live = liveEnemies[i];
                var snap = snapshot.EnemyCreatureStates[i];
                if (!string.Equals(live.Monster?.Id.Entry ?? "", snap.MonsterId, StringComparison.Ordinal))
                {
                    _lastSanityFailDetail = $"enemy{i}_id:{live.Monster?.Id.Entry}!={snap.MonsterId}";
                    return false;
                }
                if (live.CurrentHp != snap.CurrentHp || live.Block != snap.Block)
                {
                    _lastSanityFailDetail = $"enemy{i}_hp/block:{live.CurrentHp}/{live.Block}!={snap.CurrentHp}/{snap.Block}";
                    return false;
                }
                // Enemy powers were a blind spot: the original check validated
                // hp/block/id only, so a restore that dropped or mis-set an enemy
                // power (Strength/Vulnerable/Ritual/etc.) passed sanity yet fed
                // the searcher a wrong damage/defense model. Compare the power
                // multiset by (id, amount).
                var liveEnemyPowers = PowerMultiset(live.Powers);
                var snapEnemyPowers = SnapshotPowerMultiset(snap.Powers);
                if (!PowerMultisetsEqual(liveEnemyPowers, snapEnemyPowers, out var epDetail))
                {
                    _lastSanityFailDetail = $"enemy{i}_powers:{epDetail}";
                    return false;
                }
                if (aiQueues.TryGetValue(snap.MonsterId, out var aiQueue) && aiQueue.Count > 0
                    && !EnemyAiMatchesSnapshot(live, aiQueue.Dequeue(), out var aiDetail))
                {
                    _lastSanityFailDetail = $"enemy{i}_ai:{aiDetail}";
                    return false;
                }
            }

            // Player-state validation. The original check never looked at the
            // player at all, so a restore that left the player's hp/block/energy
            // or powers (Strength, Vulnerable, Frail, Dexterity, ...) wrong would
            // pass sanity and silently skew every leaf score the searcher reads.
            // The searcher consumes the ACTIVE CombatManager state, so validate
            // that source — matched against the snapshot's NetState player creature
            // (hp/block/powers) and player state (energy).
            if (!PlayerStateMatchesSnapshot(snapshot, player, out var playerDetail))
            {
                _lastSanityFailDetail = $"player:{playerDetail}";
                return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            _lastSanityFailDetail = $"exception:{ex.GetType().Name}";
            return false;
        }
    }

    private static bool EnemyAiMatchesSnapshot(
        Creature enemy,
        CombatSnapshot.EnemyAiSnapshot snapshot,
        out string detail)
    {
        detail = "";
        var monster = enemy.Monster;
        var stateMachine = monster?.MoveStateMachine;
        if (monster == null || stateMachine == null)
        {
            detail = "missing_state_machine";
            return false;
        }

        static string? StateId(object? state) =>
            AnyMember(state, "Id")?.ToString() ?? AnyMember(state, "StateId")?.ToString();

        var current = AnyMember(stateMachine, "_currentState") ?? AnyMember(stateMachine, "CurrentState");
        var initial = AnyMember(stateMachine, "_initialState") ?? AnyMember(stateMachine, "InitialState");
        var next = AnyMember(monster, "NextMove");
        var checks = new[]
        {
            (Name: "current", Actual: StateId(current), Expected: snapshot.CurrentStateId),
            (Name: "initial", Actual: StateId(initial), Expected: snapshot.InitialStateId),
            (Name: "next", Actual: StateId(next), Expected: snapshot.NextMoveId),
        };
        foreach (var check in checks)
        {
            if (!string.Equals(check.Actual, check.Expected, StringComparison.Ordinal))
            {
                detail = $"{check.Name}:{check.Actual}!={check.Expected}";
                return false;
            }
        }

        if (snapshot.PerformedFirstMove.HasValue
            && AnyMember(stateMachine, "_performedFirstMove") is bool performedFirstMove
            && performedFirstMove != snapshot.PerformedFirstMove.Value)
        {
            detail = $"performed_first:{performedFirstMove}!={snapshot.PerformedFirstMove.Value}";
            return false;
        }

        var liveLog = (AnyMember(stateMachine, "StateLog") as System.Collections.IEnumerable)
            ?.Cast<object?>()
            .Select(StateId)
            .Where(id => !string.IsNullOrWhiteSpace(id))
            .Cast<string>()
            .ToList() ?? new List<string>();
        if (!liveLog.SequenceEqual(snapshot.StateLogIds, StringComparer.Ordinal))
        {
            detail = $"state_log:[{string.Join(",", liveLog)}] != [{string.Join(",", snapshot.StateLogIds)}]";
            return false;
        }

        if (next != null
            && AnyMember(next, "_onPerform") is Delegate callback
            && string.Equals(callback.Method.Name, "UnsetMove", StringComparison.Ordinal))
        {
            detail = "next_callback_unset";
            return false;
        }
        if (snapshot.NextMoveId != null
            && snapshot.MoveStates?.TryGetValue(snapshot.NextMoveId, out var nextSnapshot) == true
            && AnyMember(next, "_performedAtLeastOnce") is bool performed
            && performed != nextSnapshot.PerformedAtLeastOnce)
        {
            detail = $"next_performed:{performed}!={nextSnapshot.PerformedAtLeastOnce}";
            return false;
        }
        return true;
    }

    // --- restore-integrity helpers (shared by in_place + full sanity) ---------

    // Multiset of live PowerModel objects as (id -> amount). Powers with amount 0
    // are dropped so a restore that leaves a zero-stack power object around does
    // not register as a mismatch against a snapshot that simply omitted it.
    private static Dictionary<string, int> PowerMultiset(System.Collections.IEnumerable? powers)
    {
        var m = new Dictionary<string, int>(StringComparer.Ordinal);
        if (powers == null) return m;
        foreach (var p in powers)
        {
            if (p == null) continue;
            var id = (GetMember(GetMember(p, "Id"), "Entry") ?? GetMember(p, "Entry"))?.ToString();
            if (string.IsNullOrEmpty(id)) continue;
            var amt = Convert.ToInt32(GetMember(p, "Amount") ?? 0);
            if (amt == 0) continue;
            m[id!] = (m.TryGetValue(id!, out var cur) ? cur : 0) + amt;
        }
        return m;
    }

    private static Dictionary<string, int> SnapshotPowerMultiset(IEnumerable<CombatSnapshot.EnemyPowerSnapshot>? powers)
    {
        var m = new Dictionary<string, int>(StringComparer.Ordinal);
        if (powers == null) return m;
        foreach (var p in powers)
        {
            if (p == null || string.IsNullOrEmpty(p.Id) || p.Amount == 0) continue;
            m[p.Id] = (m.TryGetValue(p.Id, out var cur) ? cur : 0) + p.Amount;
        }
        return m;
    }

    private static bool PowerMultisetsEqual(Dictionary<string, int> a, Dictionary<string, int> b, out string detail)
    {
        detail = "";
        if (a.Count != b.Count)
        {
            detail = $"count:{a.Count}!={b.Count}";
            return false;
        }
        foreach (var kv in a)
        {
            if (!b.TryGetValue(kv.Key, out var bv) || bv != kv.Value)
            {
                detail = $"{kv.Key}:{kv.Value}!={(b.TryGetValue(kv.Key, out var x) ? x.ToString() : "missing")}";
                return false;
            }
        }
        return true;
    }

    // Validate the live player's hp/block/energy/powers against the snapshot.
    // Snapshot player hp/block/powers live on the NetState player creature (the
    // one whose playerId is set); energy lives on the NetState player state.
    private bool PlayerStateMatchesSnapshot(CombatSnapshot snapshot, Player player, out string detail)
    {
        detail = "";
        var creature = player.Creature;
        var pcs = player.PlayerCombatState;
        if (creature == null || pcs == null)
        {
            detail = "live_null";
            return false;
        }

        var snapHp = ExtractSnapshotPlayerHp(snapshot.NetState);
        if (snapHp.HasValue && creature.CurrentHp != snapHp.Value)
        {
            detail = $"hp:{creature.CurrentHp}!={snapHp.Value}";
            return false;
        }
        var snapBlock = ExtractSnapshotPlayerBlock(snapshot.NetState);
        if (snapBlock.HasValue && creature.Block != snapBlock.Value)
        {
            detail = $"block:{creature.Block}!={snapBlock.Value}";
            return false;
        }
        var snapEnergy = ExtractSnapshotPlayerEnergy(snapshot.NetState);
        if (snapEnergy.HasValue && pcs.Energy != snapEnergy.Value)
        {
            detail = $"energy:{pcs.Energy}!={snapEnergy.Value}";
            return false;
        }
        var livePowers = PowerMultiset(creature.Powers);
        var snapPowers = ExtractSnapshotPlayerPowers(snapshot.NetState);
        if (snapPowers != null && !PowerMultisetsEqual(livePowers, snapPowers, out var pd))
        {
            detail = $"powers:{pd}";
            return false;
        }
        return true;
    }

    private static int? ExtractSnapshotPlayerHp(object? snapshot)
    {
        var creatures = GetMember(snapshot, "Creatures") as System.Collections.IEnumerable;
        if (creatures == null) return null;
        foreach (var c in creatures)
            if (GetField(c, "playerId") != null) return GetIntField(c, "currentHp");
        return null;
    }

    private static Dictionary<string, int>? ExtractSnapshotPlayerPowers(object? snapshot)
    {
        var creatures = GetMember(snapshot, "Creatures") as System.Collections.IEnumerable;
        if (creatures == null) return null;
        foreach (var c in creatures)
        {
            if (GetField(c, "playerId") == null) continue;
            var powers = GetField(c, "powers") as System.Collections.IEnumerable;
            var m = new Dictionary<string, int>(StringComparer.Ordinal);
            if (powers != null)
            {
                foreach (var p in powers)
                {
                    if (p == null) continue;
                    var id = (GetMember(GetMember(p, "id"), "Entry") ?? GetMember(p, "Entry"))?.ToString();
                    if (string.IsNullOrEmpty(id)) continue;
                    var amt = Convert.ToInt32(GetMember(p, "amount") ?? 0);
                    if (amt == 0) continue;
                    m[id!] = (m.TryGetValue(id!, out var cur) ? cur : 0) + amt;
                }
            }
            return m;
        }
        return null;
    }

    private string _lastSanityFailDetail = "";

    // Compares the CURRENT live combat's living enemy line-up against the snapshot's,
    // BEFORE any reconcile/apply mutates the live list. Used as an in_place
    // precondition: a reused worker whose live combat is a different encounter
    // must not be restored in place (see TryRestoreCombatSnapshotInPlace). The
    // encounter identity is the multiset of enemy monster ids; HP/block/turn
    // differences are exactly what a same-encounter in_place restore repairs, so
    // they are intentionally NOT compared here.
    private bool LiveCombatMatchesSnapshotEncounter(CombatRoom room, CombatSnapshot snapshot, out string detail)
    {
        detail = "";
        var combatState = CombatManager.Instance.DebugOnlyGetState() ?? room.CombatState;
        if (combatState == null)
        {
            detail = "no_combat_state";
            return false;
        }
        var liveIds = (combatState.Enemies ?? Enumerable.Empty<Creature>())
            .Where(e => e != null && e.IsAlive)
            .Select(e => e.Monster?.Id.Entry ?? "")
            .OrderBy(s => s, StringComparer.Ordinal)
            .ToList();
        var snapIds = snapshot.EnemyCreatureStates
            .Select(e => e.MonsterId ?? "")
            .OrderBy(s => s, StringComparer.Ordinal)
            .ToList();
        // Same-encounter test, robust to mid-fight enemy death. During a deep
        // search a reused worker's live combat may have FEWER enemies than the
        // snapshot — a replayed line killed a low-HP enemy (SLIMES/NIBBITS), so
        // the engine removed it from CombatState.Enemies. That is NOT a different
        // encounter: ReconcileEnemyListToSnapshot re-adds the dead enemies from
        // ModelDb to match the snapshot exactly (the same thing a full restore's
        // EnterRoom+reconcile does). The only thing the guard must still reject is
        // a genuinely DIFFERENT encounter (a worker reused across combats, e.g.
        // NIBBITS -> SHRINKER_BEETLE), which would let in_place silently corrupt
        // an incompatible live combat. So the test is multiset-SUBSET, not
        // multiset-equality: every live enemy id must appear (with multiplicity)
        // in the snapshot. If the live roster has an id the snapshot lacks, or has
        // MORE of an id than the snapshot, it is a different combat -> reject.
        var snapCounts = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var id in snapIds)
            snapCounts[id] = snapCounts.GetValueOrDefault(id) + 1;
        var liveCounts = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var id in liveIds)
            liveCounts[id] = liveCounts.GetValueOrDefault(id) + 1;
        foreach (var kv in liveCounts)
        {
            if (snapCounts.GetValueOrDefault(kv.Key) < kv.Value)
            {
                detail = $"ids:[{string.Join(",", liveIds)}]!~[{string.Join(",", snapIds)}]";
                return false;
            }
        }
        return true;
    }

    private static double timeMs() => Stopwatch.GetTimestamp() * 1000.0 / Stopwatch.Frequency;

    private static CombatSnapshot.SerializedEnvelope BuildCombatSnapshotEnvelope(
        CombatSnapshot snapshot,
        string? idOverride = null)
    {
        return new CombatSnapshot.SerializedEnvelope
        {
            Id = idOverride ?? snapshot.Id,
            CharacterName = snapshot.CharacterName,
            AscensionLevel = snapshot.AscensionLevel,
            Seed = snapshot.Seed,
            ActIndex = snapshot.ActIndex,
            ActFloor = snapshot.ActFloor,
            BossEncounterId = snapshot.BossEncounterId,
            RoomJson = System.Text.Json.JsonSerializer.Serialize(snapshot.Room, SnapshotJsonOpts),
            PlayerJson = System.Text.Json.JsonSerializer.Serialize(snapshot.Player, SnapshotJsonOpts),
            NetState = BuildPlainNetState(snapshot.NetState, snapshot.RuntimeCardCosts),
            EnemyCreatureStates = snapshot.EnemyCreatureStates,
            EnemyAiStates = snapshot.EnemyAiStates,
            RunRngStates = snapshot.RunRngStates,
            PlayerRngStates = snapshot.PlayerRngStates,
            RoundNumber = snapshot.RoundNumber,
            CurrentSide = snapshot.CurrentSide,
            RelicStates = snapshot.RelicStates,
            HookStates = snapshot.HookStates,
            PlayerCombatState = snapshot.PlayerCombatState,
            PlayerExtraState = snapshot.PlayerExtraState,
            CombatHistoryEntries = snapshot.CombatHistoryEntries,
            ActivePowerRefs = snapshot.ActivePowerRefs,
        };
    }

    private static string ComputeCombatSnapshotFingerprint(CombatSnapshot snapshot)
    {
        // Snapshot ids are process-local handles, not state. Everything else in
        // the export envelope is required to reconstruct the combat and is part
        // of the strict identity used by the search DAG.
        var envelope = BuildCombatSnapshotEnvelope(snapshot, idOverride: "");
        var canonical = System.Text.Json.JsonSerializer.Serialize(envelope, SnapshotJsonOpts);
        var digest = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(canonical))).ToLowerInvariant();
        return $"sts2-combat-snapshot-v1:{digest}";
    }

    private static string ComputeCombatSnapshotSemanticFingerprint(CombatSnapshot snapshot)
    {
        // Preserve the complete restorable envelope and erase only ordering in
        // piles whose existing cards are proven unobserved by the Python search
        // certificate. Draw and hand order remain exact. PileType values are
        // engine enum values: 1=Draw, 2=Hand, 3=Discard, 4=Exhaust, 5=Play.
        var envelope = BuildCombatSnapshotEnvelope(snapshot, idOverride: "");
        foreach (var player in envelope.NetState.Players)
        {
            foreach (var pile in player.piles)
            {
                if (pile.pileType is not (3 or 4 or 5))
                    continue;
                pile.cards = pile.cards
                    .OrderBy(card => System.Text.Json.JsonSerializer.Serialize(
                        card, SnapshotJsonOpts), StringComparer.Ordinal)
                    .ToList();
            }
        }
        var semanticNode = System.Text.Json.Nodes.JsonNode.Parse(
            System.Text.Json.JsonSerializer.Serialize(envelope, SnapshotJsonOpts))
            ?.AsObject() ?? throw new InvalidOperationException("Could not canonicalize combat snapshot");
        if (semanticNode["PlayerJson"] is System.Text.Json.Nodes.JsonValue playerJsonValue
            && playerJsonValue.TryGetValue<string>(out var playerJson)
            && System.Text.Json.Nodes.JsonNode.Parse(playerJson) is System.Text.Json.Nodes.JsonObject playerNode
            && playerNode["extra_fields"] is System.Text.Json.Nodes.JsonObject extraFields)
        {
            // These are cumulative run telemetry counters. Candidate replay
            // increments them, but the combat engine never consults them when
            // resolving later cards, intents, RNG, powers, or turn transitions.
            // Keep them in the strict fingerprint; exclude only from the DAG
            // identity so commutative combat actions can converge safely.
            extraFields.Remove("damage_dealt");
            extraFields.Remove("debuffs_applied");
            semanticNode["PlayerJson"] = playerNode.ToJsonString(SnapshotJsonOpts);
        }
        if (semanticNode["PlayerExtraState"] is System.Text.Json.Nodes.JsonObject playerExtraState
            && playerExtraState["Fields"] is System.Text.Json.Nodes.JsonArray playerExtraFields)
        {
            // The same telemetry counters also exist on the captured live
            // PlayerExtraFields object. They must remain in the strict snapshot
            // so a parent restore is exact, but including them here would split
            // otherwise equivalent DAG nodes solely because their action order
            // produced different cumulative reporting values.
            for (var index = playerExtraFields.Count - 1; index >= 0; index--)
            {
                if (playerExtraFields[index] is not System.Text.Json.Nodes.JsonObject field
                    || field["Name"]?.GetValue<string>() is not string fieldName)
                    continue;
                if (fieldName is "<DamageDealt>k__BackingField" or "<DebuffsApplied>k__BackingField")
                    playerExtraFields.RemoveAt(index);
            }
        }
        var canonical = semanticNode.ToJsonString(SnapshotJsonOpts);
        var digest = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(canonical))).ToLowerInvariant();
        return $"sts2-combat-semantic-v1:{digest}";
    }

    private static Dictionary<string, object?> CompactBatchActionResult(
        Dictionary<string, object?> result)
    {
        return SelectBatchFields(
            result,
            "type", "success", "decision", "message", "headless_execute_ms",
            "headless_wait_profile");
    }

    private static Dictionary<string, object?> CompactBatchRestoreResult(
        Dictionary<string, object?> result)
    {
        if (result.TryGetValue("type", out var type) &&
            string.Equals(type?.ToString(), "error", StringComparison.Ordinal))
            return result;
        return SelectBatchFields(
            result,
            "type", "success", "decision", "message", "restored_snapshot_id",
            "restore_mode", "restore_timing_ms", "restore_sanity_warning");
    }

    private static Dictionary<string, object?> SelectBatchFields(
        Dictionary<string, object?> source,
        params string[] fields)
    {
        var compact = new Dictionary<string, object?> { ["compact"] = true };
        foreach (var field in fields)
            if (source.TryGetValue(field, out var value))
                compact[field] = value;
        return compact;
    }

    public Dictionary<string, object?> ExportCombatSnapshot(string snapshotId)
    {
        try
        {
            if (!_combatSnapshots.TryGetValue(snapshotId, out var snapshot))
                return Error($"Unknown combat snapshot: {snapshotId}");

            var envelope = BuildCombatSnapshotEnvelope(snapshot);
            var json = System.Text.Json.JsonSerializer.Serialize(envelope, SnapshotJsonOpts);
            return new Dictionary<string, object?>
            {
                ["type"] = "combat_snapshot_exported",
                ["success"] = true,
                ["snapshot_id"] = snapshotId,
                ["state_fingerprint"] = GetCombatSnapshotFingerprint(snapshot),
                ["state_fingerprint_schema"] = "sts2-combat-snapshot-v1",
                ["semantic_state_fingerprint"] = GetCombatSnapshotSemanticFingerprint(snapshot),
                ["semantic_state_fingerprint_schema"] = "sts2-combat-semantic-v1",
                ["snapshot_json"] = json,
                ["size"] = json.Length,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("ExportCombatSnapshot failed", ex);
        }
    }

    public Dictionary<string, object?> ImportCombatSnapshot(string snapshotJson, string? snapshotId = null)
    {
        try
        {
            var envelope = System.Text.Json.JsonSerializer.Deserialize<CombatSnapshot.SerializedEnvelope>(snapshotJson, SnapshotJsonOpts);
            if (envelope == null)
                return Error("Could not deserialize combat snapshot");

            var room = System.Text.Json.JsonSerializer.Deserialize<SerializableRoom>(envelope.RoomJson, SnapshotJsonOpts);
            var player = System.Text.Json.JsonSerializer.Deserialize<SerializablePlayer>(envelope.PlayerJson, SnapshotJsonOpts);
            if (room == null || player == null || envelope.NetState == null)
                return Error("Combat snapshot envelope missing room/player/netstate");

            var id = string.IsNullOrWhiteSpace(snapshotId) ? envelope.Id : snapshotId!;
            var importedSnapshot = new CombatSnapshot
            {
                Id = id,
                CharacterName = envelope.CharacterName,
                AscensionLevel = envelope.AscensionLevel,
                Seed = envelope.Seed,
                ActIndex = envelope.ActIndex,
                ActFloor = envelope.ActFloor,
                BossEncounterId = envelope.BossEncounterId,
                Room = room,
                Player = player,
                NetState = envelope.NetState,
                EnemyCreatureStates = envelope.EnemyCreatureStates,
                EnemyAiStates = envelope.EnemyAiStates,
                RunRngStates = envelope.RunRngStates,
                PlayerRngStates = envelope.PlayerRngStates,
                RoundNumber = envelope.RoundNumber,
                CurrentSide = envelope.CurrentSide,
                RelicStates = envelope.RelicStates,
                HookStates = envelope.HookStates,
                PlayerCombatState = envelope.PlayerCombatState,
                PlayerExtraState = envelope.PlayerExtraState,
                CombatHistoryEntries = envelope.CombatHistoryEntries,
                ActivePowerRefs = envelope.ActivePowerRefs,
            };
            _combatSnapshots[id] = importedSnapshot;
            return new Dictionary<string, object?>
            {
                ["type"] = "combat_snapshot_imported",
                ["success"] = true,
                ["snapshot_id"] = id,
                ["state_fingerprint"] = GetCombatSnapshotFingerprint(importedSnapshot),
                ["state_fingerprint_schema"] = "sts2-combat-snapshot-v1",
                ["semantic_state_fingerprint"] = GetCombatSnapshotSemanticFingerprint(importedSnapshot),
                ["semantic_state_fingerprint_schema"] = "sts2-combat-semantic-v1",
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("ImportCombatSnapshot failed", ex);
        }
    }

    private static CombatSnapshot.PlainNetState BuildPlainNetState(
        object netStateObj,
        Dictionary<int, List<CombatSnapshot.PlainRuntimeEnergyCost>>? runtimeCardCosts = null)
    {
        var creatures = (GetMember(netStateObj, "Creatures") as System.Collections.IEnumerable ?? Enumerable.Empty<object?>())
            .Cast<object?>()
            .Where(c => c != null)
            .Select(c => new CombatSnapshot.PlainCreatureState
            {
                monsterId = ToPlainModelId(GetMember(c!, "monsterId") ?? GetMember(c!, "MonsterId")),
                playerId = GetMember(c!, "playerId") as ulong? ?? (GetMember(c!, "playerId") != null ? Convert.ToUInt64(GetMember(c!, "playerId")) : null),
                currentHp = Convert.ToInt32(GetMember(c!, "currentHp") ?? 0),
                maxHp = Convert.ToInt32(GetMember(c!, "maxHp") ?? 0),
                block = Convert.ToInt32(GetMember(c!, "block") ?? 0),
                powers = ((GetMember(c!, "powers") as System.Collections.IEnumerable) ?? Enumerable.Empty<object?>())
                    .Cast<object?>()
                    .Where(p => p != null)
                    .Select(p => new CombatSnapshot.PlainPowerState
                    {
                        id = ToPlainModelId(GetMember(p!, "id")) ?? new CombatSnapshot.PlainModelId { Category = "POWER", Entry = "" },
                        amount = Convert.ToInt32(GetMember(p!, "amount") ?? 0),
                    })
                    .Where(p => !string.IsNullOrWhiteSpace(p.id.Entry))
                    .ToList(),
            })
            .ToList();

        var players = (GetMember(netStateObj, "Players") as System.Collections.IEnumerable ?? Enumerable.Empty<object?>())
            .Cast<object?>()
            .Where(p => p != null)
            .Select(p => new CombatSnapshot.PlainPlayerState
            {
                playerId = GetMember(p!, "playerId") as ulong? ?? (GetMember(p!, "playerId") != null ? Convert.ToUInt64(GetMember(p!, "playerId")) : null),
                characterId = ToPlainModelId(GetMember(p!, "characterId")),
                energy = Convert.ToInt32(GetMember(p!, "energy") ?? 0),
                stars = Convert.ToInt32(GetMember(p!, "stars") ?? 0),
                maxStars = Convert.ToInt32(GetMember(p!, "maxStars") ?? 0),
                maxPotionCount = Convert.ToInt32(GetMember(p!, "maxPotionCount") ?? 0),
                gold = Convert.ToInt32(GetMember(p!, "gold") ?? 0),
                piles = ((GetMember(p!, "piles") as System.Collections.IEnumerable) ?? Enumerable.Empty<object?>())
                    .Cast<object?>()
                    .Where(ps => ps != null)
                    .Select(ps => new CombatSnapshot.PlainPileState
                    {
                        pileType = Convert.ToInt32(GetMember(ps!, "pileType") ?? 0),
                        cards = ((GetMember(ps!, "cards") as System.Collections.IEnumerable) ?? Enumerable.Empty<object?>())
                            .Cast<object?>()
                            .Where(cs => cs != null)
                            .Select((cs, index) => new CombatSnapshot.PlainCardState
                            {
                                card = BuildPlainSerializableCard(GetMember(cs!, "card")!),
                                affliction = ToPlainModelId(GetMember(cs!, "affliction")),
                                afflictionCount = Convert.ToInt32(GetMember(cs!, "afflictionCount") ?? 0),
                                energyCost = BuildPlainEnergyCost(GetMember(cs!, "energyCost")),
                                runtimeEnergyCost = BuildPlainRuntimeEnergyCost(
                                    GetMember(cs!, "runtimeEnergyCost")
                                    ?? (runtimeCardCosts != null
                                        && runtimeCardCosts.TryGetValue(Convert.ToInt32(GetMember(ps!, "pileType") ?? 0), out var costs)
                                        && index < costs.Count ? costs[index] : null)),
                                keywords = ((GetMember(cs!, "keywords") as System.Collections.IEnumerable) ?? Enumerable.Empty<object?>())
                                    .Cast<object?>().Where(k => k != null).Select(k => k!.ToString()!).ToList(),
                            })
                            .ToList(),
                    })
                    .ToList(),
                rngSet = new CombatSnapshot.PlainCountersContainer
                {
                    Counters = ToCounterDictionary(GetMember(GetMember(p!, "rngSet"), "Counters")),
                },
            })
            .ToList();

        if (runtimeCardCosts != null)
        {
            foreach (var player in players)
            foreach (var pile in player.piles)
            {
                if (!runtimeCardCosts.TryGetValue(pile.pileType, out var costs)
                    || costs.Count != pile.cards.Count
                    || pile.cards.Any(card => card.runtimeEnergyCost == null))
                    throw new InvalidOperationException($"Runtime card-cost capture mismatch in pile {pile.pileType}");
            }
        }

        var rngCounters = ToCounterDictionary(GetMember(GetMember(netStateObj, "Rng"), "Counters"));

        return new CombatSnapshot.PlainNetState
        {
            Creatures = creatures,
            Players = players,
            Rng = new CombatSnapshot.PlainNetRngState
            {
                Seed = GetMember(GetMember(netStateObj, "Rng"), "Seed")?.ToString(),
                Counters = rngCounters,
            },
        };
    }

    private static CombatSnapshot.PlainEnergyCost? BuildPlainEnergyCost(object? energyCost)
    {
        if (energyCost == null)
            return null;
        if (energyCost is IConvertible)
            return new CombatSnapshot.PlainEnergyCost { ResolvedValue = Convert.ToInt32(energyCost) };
        return new CombatSnapshot.PlainEnergyCost
        {
            Value = GetMember(energyCost, "Value") != null ? Convert.ToInt32(GetMember(energyCost, "Value")) : null,
            ResolvedValue = GetMember(energyCost, "ResolvedValue") != null ? Convert.ToInt32(GetMember(energyCost, "ResolvedValue")) : null,
        };
    }

    private static CombatSnapshot.PlainRuntimeEnergyCost? BuildPlainRuntimeEnergyCost(object? value)
    {
        if (value == null)
            return null;
        return new CombatSnapshot.PlainRuntimeEnergyCost
        {
            Base = Convert.ToInt32(GetMember(value, "Base") ?? 0),
            Canonical = Convert.ToInt32(GetMember(value, "Canonical") ?? 0),
            CostsX = Convert.ToBoolean(GetMember(value, "CostsX") ?? false),
            CapturedXValue = Convert.ToInt32(GetMember(value, "CapturedXValue") ?? 0),
            WasJustUpgraded = Convert.ToBoolean(GetMember(value, "WasJustUpgraded") ?? false),
            LocalModifiers = ((GetMember(value, "LocalModifiers") as System.Collections.IEnumerable)
                ?? Enumerable.Empty<object?>()).Cast<object?>().Where(item => item != null)
                .Select(item => new CombatSnapshot.PlainLocalCostModifier
                {
                    Amount = Convert.ToInt32(GetMember(item, "Amount") ?? 0),
                    Type = Convert.ToInt32(GetMember(item, "Type") ?? 0),
                    Expiration = Convert.ToInt32(GetMember(item, "Expiration") ?? 0),
                    IsReduceOnly = Convert.ToBoolean(GetMember(item, "IsReduceOnly") ?? false),
                }).ToList(),
        };
    }

    private static Dictionary<int, List<CombatSnapshot.PlainRuntimeEnergyCost>> CaptureRuntimeCardCosts(Player player)
    {
        return player.PlayerCombatState.AllPiles.ToDictionary(
            pile => Convert.ToInt32(pile.Type),
            pile => pile.Cards.Select(card =>
            {
                var cost = card.EnergyCost ?? throw new InvalidOperationException("Runtime card has no energy cost");
                var rawBase = AnyMember(cost, "_base")
                    ?? throw new InvalidOperationException("Card cost base is unavailable");
                var modifiers = AnyMember(cost, "_localModifiers") as System.Collections.IEnumerable
                    ?? throw new InvalidOperationException("Card cost modifiers are unavailable");
                return new CombatSnapshot.PlainRuntimeEnergyCost
                {
                    Base = Convert.ToInt32(rawBase),
                    Canonical = cost.Canonical,
                    CostsX = cost.CostsX,
                    CapturedXValue = Convert.ToInt32(AnyMember(cost, "_capturedXValue") ?? 0),
                    WasJustUpgraded = cost.WasJustUpgraded,
                    LocalModifiers = modifiers.Cast<object>().Select(modifier =>
                        new CombatSnapshot.PlainLocalCostModifier
                        {
                            Amount = Convert.ToInt32(GetMember(modifier, "Amount") ?? 0),
                            Type = Convert.ToInt32(GetMember(modifier, "Type") ?? 0),
                            Expiration = Convert.ToInt32(GetMember(modifier, "Expiration") ?? 0),
                            IsReduceOnly = Convert.ToBoolean(GetMember(modifier, "IsReduceOnly") ?? false),
                        }).ToList(),
                };
            }).ToList());
    }

    private static CombatSnapshot.PlainSerializableCard BuildPlainSerializableCard(object serializableCard)
    {
        return new CombatSnapshot.PlainSerializableCard
        {
            id = ToPlainModelId(GetMember(serializableCard, "id") ?? GetMember(serializableCard, "Id")) ?? new CombatSnapshot.PlainModelId { Category = "CARD", Entry = "" },
            floor_added_to_deck = Convert.ToInt32(GetMember(serializableCard, "floor_added_to_deck") ?? GetMember(serializableCard, "FloorAddedToDeck") ?? 0),
            CurrentUpgradeLevel = GetMember(serializableCard, "CurrentUpgradeLevel") != null
                ? Convert.ToInt32(GetMember(serializableCard, "CurrentUpgradeLevel"))
                : null,
            enchantment = BuildPlainSerializableEnchantment(
                GetMember(serializableCard, "enchantment") ?? GetMember(serializableCard, "Enchantment")),
        };
    }

    private static CombatSnapshot.PlainSerializableEnchantment? BuildPlainSerializableEnchantment(object? serializableEnchantment)
    {
        if (serializableEnchantment == null)
            return null;
        var id = ToPlainModelId(
            GetMember(serializableEnchantment, "id") ?? GetMember(serializableEnchantment, "Id"));
        if (id == null || string.IsNullOrWhiteSpace(id.Entry))
            return null;
        return new CombatSnapshot.PlainSerializableEnchantment
        {
            id = id,
            amount = Convert.ToInt32(
                GetMember(serializableEnchantment, "amount") ?? GetMember(serializableEnchantment, "Amount") ?? 0),
            props = (GetMember(serializableEnchantment, "props")
                ?? GetMember(serializableEnchantment, "Props")) as SavedProperties,
        };
    }

    private static CombatSnapshot.PlainModelId? ToPlainModelId(object? modelId)
    {
        if (modelId == null)
            return null;
        return new CombatSnapshot.PlainModelId
        {
            Category = GetMember(modelId, "Category")?.ToString(),
            Entry = GetMember(modelId, "Entry")?.ToString(),
        };
    }

    private static Dictionary<string, int> ToCounterDictionary(object? counters)
    {
        var result = new Dictionary<string, int>(StringComparer.Ordinal);
        if (counters == null)
            return result;

        if (counters is System.Collections.IDictionary dict)
        {
            foreach (System.Collections.DictionaryEntry entry in dict)
            {
                if (entry.Key == null)
                    continue;
                result[entry.Key.ToString()!] = Convert.ToInt32(entry.Value ?? 0);
            }
            return result;
        }

        if (counters is System.Collections.IEnumerable enumerable)
        {
            foreach (var item in enumerable)
            {
                if (item == null)
                    continue;
                var key = GetMember(item, "Key");
                if (key == null)
                    continue;
                var value = GetMember(item, "Value");
                result[key.ToString()!] = Convert.ToInt32(value ?? 0);
            }
        }

        return result;
    }

    public Dictionary<string, object?> InspectCombatMembership()
    {
        try
        {
            var payload = TryInspectCombatMembershipForError();
            return new Dictionary<string, object?>
            {
                ["type"] = "combat_membership",
                ["success"] = true,
                ["membership"] = payload,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectCombatMembership failed", ex);
        }
    }

    public Dictionary<string, object?> InspectPowerMethods()
    {
        try
        {
            static List<Dictionary<string, object?>> DescribeMethods(Type t) =>
                t.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .Where(m => m.Name.Contains("Power", StringComparison.OrdinalIgnoreCase) ||
                                m.Name.Contains("Hook", StringComparison.OrdinalIgnoreCase) ||
                                m.Name.Contains("Apply", StringComparison.OrdinalIgnoreCase) ||
                                m.Name.Contains("Remove", StringComparison.OrdinalIgnoreCase) ||
                                m.Name.Contains("Internal", StringComparison.OrdinalIgnoreCase))
                    .Select(m => new Dictionary<string, object?>
                    {
                        ["name"] = m.Name,
                        ["return"] = m.ReturnType.FullName,
                        ["parameters"] = m.GetParameters()
                            .Select(p => $"{p.ParameterType.FullName} {p.Name}")
                            .ToList(),
                    })
                    .OrderBy(m => m["name"]?.ToString())
                    .ToList();

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_power_methods_result",
                ["success"] = true,
                ["creature_methods"] = DescribeMethods(typeof(Creature)),
                ["combat_state_methods"] = DescribeMethods(typeof(CombatState)),
                ["power_model_methods"] = DescribeMethods(typeof(PowerModel)),
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectPowerMethods failed", ex);
        }
    }

    public Dictionary<string, object?> GetRngSnapshot()
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");

            static SortedDictionary<string, object?> DescribeSet(
                IEnumerable<CombatSnapshot.RngStateSnapshot> snapshots)
            {
                var streams = new SortedDictionary<string, object?>(StringComparer.Ordinal);
                foreach (var state in snapshots.OrderBy(value => value.Name, StringComparer.Ordinal))
                {
                    streams[state.Name] = new SortedDictionary<string, object?>(StringComparer.Ordinal)
                    {
                        ["counter"] = state.Counter,
                        ["seed"] = state.Seed,
                        ["s0"] = state.S0,
                        ["s1"] = state.S1,
                        ["s2"] = state.S2,
                        ["s3"] = state.S3,
                    };
                }
                return streams;
            }

            static bool Complete(SortedDictionary<string, object?> streams)
            {
                if (streams.Count == 0)
                    return false;
                return streams.Values.All(value => value is SortedDictionary<string, object?> row
                    && row["counter"] != null && row["seed"] != null
                    && row["s0"] != null && row["s1"] != null
                    && row["s2"] != null && row["s3"] != null);
            }

            var runStreams = DescribeSet(CaptureDetailedRngStates(_runState.Rng));
            var players = new List<object?>();
            foreach (var player in _runState.Players)
            {
                var streams = DescribeSet(CaptureDetailedRngStates(player.PlayerRng));
                players.Add(new SortedDictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["net_id"] = player.NetId.ToString(),
                    ["streams"] = streams,
                });
            }
            var payload = new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["schema_version"] = 1,
                ["complete"] = Complete(runStreams) && players.All(value =>
                    value is SortedDictionary<string, object?> player
                    && player["streams"] is SortedDictionary<string, object?> streams
                    && Complete(streams)),
                ["run_seed"] = _runState.Rng.StringSeed,
                ["run_streams"] = runStreams,
                ["players"] = players,
            };
            var canonical = System.Text.Json.JsonSerializer.Serialize(payload, SnapshotJsonOpts);
            payload["digest_sha256"] = Convert.ToHexString(
                System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(canonical)))
                .ToLowerInvariant();
            return new Dictionary<string, object?>
            {
                ["type"] = "rng_snapshot_result",
                ["success"] = true,
                ["rng"] = payload,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("GetRngSnapshot failed", ex);
        }
    }

    public Dictionary<string, object?> InspectRngState()
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");

            static List<Dictionary<string, object?>> DescribeRngDictionary(object? rngSet)
            {
                var rows = new List<Dictionary<string, object?>>();
                if (rngSet == null)
                    return rows;
                var dict = rngSet.GetType().GetField("_rngs", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(rngSet)
                    as System.Collections.IDictionary;
                if (dict == null)
                    return rows;

                foreach (System.Collections.DictionaryEntry entry in dict)
                {
                    var value = entry.Value;
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["key"] = entry.Key?.ToString(),
                        ["value_type"] = value?.GetType().FullName,
                        ["members"] = value != null ? value.GetType()
                            .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                            .Where(f => f.Name.Contains("count", StringComparison.OrdinalIgnoreCase) ||
                                        f.Name.Contains("seed", StringComparison.OrdinalIgnoreCase) ||
                                        f.Name.Contains("state", StringComparison.OrdinalIgnoreCase) ||
                                        f.Name.Contains("index", StringComparison.OrdinalIgnoreCase))
                            .Select(f => new Dictionary<string, object?>
                            {
                                ["name"] = f.Name,
                                ["type"] = f.FieldType.FullName,
                                ["value"] = f.GetValue(value)?.ToString(),
                            })
                            .ToList() : null,
                    });
                }

                return rows;
            }

            static List<Dictionary<string, object?>> DescribeMembers(object obj)
            {
                var rows = new List<Dictionary<string, object?>>();
                var t = obj.GetType();
                foreach (var field in t.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                             .Where(f => f.Name.Contains("rng", StringComparison.OrdinalIgnoreCase) ||
                                         f.Name.Contains("counter", StringComparison.OrdinalIgnoreCase) ||
                                         f.Name.Contains("seed", StringComparison.OrdinalIgnoreCase)))
                {
                    object? value = null;
                    try { value = field.GetValue(obj); } catch { }
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["member_kind"] = "field",
                        ["name"] = field.Name,
                        ["type"] = field.FieldType.FullName,
                        ["value_type"] = value?.GetType().FullName,
                        ["value"] = value?.ToString(),
                    });
                }

                foreach (var prop in t.GetProperties(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                             .Where(p => p.Name.Contains("rng", StringComparison.OrdinalIgnoreCase) ||
                                         p.Name.Contains("counter", StringComparison.OrdinalIgnoreCase) ||
                                         p.Name.Contains("seed", StringComparison.OrdinalIgnoreCase)))
                {
                    object? value = null;
                    try { value = prop.GetValue(obj); } catch { }
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["member_kind"] = "property",
                        ["name"] = prop.Name,
                        ["type"] = prop.PropertyType.FullName,
                        ["value_type"] = value?.GetType().FullName,
                        ["value"] = value?.ToString(),
                    });
                }

                return rows;
            }

            static Dictionary<string, object?> DescribeObject(string label, object obj)
            {
                var members = DescribeMembers(obj);
                var nested = new List<Dictionary<string, object?>>();
                foreach (var member in members)
                {
                    var name = member["name"]?.ToString() ?? "";
                    object? value = null;
                    try
                    {
                        if (member["member_kind"]?.ToString() == "field")
                            value = obj.GetType().GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(obj);
                        else
                            value = obj.GetType().GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(obj);
                    }
                    catch { }

                    if (value is System.Collections.IDictionary dict)
                    {
                        var entries = new List<Dictionary<string, object?>>();
                        foreach (System.Collections.DictionaryEntry entry in dict)
                        {
                            var entryValue = entry.Value;
                            entries.Add(new Dictionary<string, object?>
                            {
                                ["key"] = entry.Key?.ToString(),
                                ["value_type"] = entryValue?.GetType().FullName,
                                ["members"] = entryValue != null ? DescribeMembers(entryValue) : null,
                            });
                        }
                        if (entries.Count > 0)
                        {
                            nested.Add(new Dictionary<string, object?>
                            {
                                ["parent_member"] = name,
                                ["dictionary_entries"] = entries,
                            });
                        }
                    }
                    else if (value != null && !value.GetType().FullName!.StartsWith("System.", StringComparison.Ordinal))
                    {
                        var sub = DescribeMembers(value);
                        if (sub.Count > 0)
                        {
                            nested.Add(new Dictionary<string, object?>
                            {
                                ["parent_member"] = name,
                                ["nested_members"] = sub,
                            });
                        }
                    }
                }

                return new Dictionary<string, object?>
                {
                    ["object"] = label,
                    ["members"] = members,
                    ["nested"] = nested,
                };
            }

            var rows = new List<Dictionary<string, object?>>
            {
                DescribeObject("run_state", _runState)
            };

            rows.Add(new Dictionary<string, object?>
            {
                ["object"] = "run_state_rng_entries",
                ["entries"] = DescribeRngDictionary(_runState.Rng),
            });

            var player = _runState.Players.FirstOrDefault();
            if (player != null)
            {
                rows.Add(DescribeObject("player", player));
                rows.Add(new Dictionary<string, object?>
                {
                    ["object"] = "player_rng_entries",
                    ["entries"] = DescribeRngDictionary(player.PlayerRng),
                });
                if (player.PlayerCombatState != null)
                {
                    rows.Add(DescribeObject("player_combat_state", player.PlayerCombatState));
                }
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_rng_state_result",
                ["success"] = true,
                ["objects"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectRngState failed", ex);
        }
    }

    public Dictionary<string, object?> InspectTypeMethods(string typeName)
    {
        try
        {
            EnsureModelDbInitialized();
            var asm = typeof(RunState).Assembly;
            var type = asm.GetType(typeName, throwOnError: false, ignoreCase: false);
            if (type == null)
                return Error($"Type not found: {typeName}");
            var methods = type
                .GetMethods(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)
                .Select(m => new Dictionary<string, object?>
                {
                    ["name"] = m.Name,
                    ["static"] = m.IsStatic,
                    ["public"] = m.IsPublic,
                    ["return_type"] = m.ReturnType.FullName,
                    ["parameters"] = m.GetParameters().Select(p => $"{p.ParameterType.FullName} {p.Name}").ToList(),
                })
                .OrderBy(m => m["name"]?.ToString())
                .ToList();
            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_type_methods_result",
                ["success"] = true,
                ["target_type"] = type.FullName,
                ["methods"] = methods,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectTypeMethods failed", ex);
        }
    }

    public Dictionary<string, object?> InspectCards(IReadOnlyList<string> cardIds)
    {
        try
        {
            EnsureModelDbInitialized();
            var cards = new List<Dictionary<string, object?>>();
            var missing = new List<string>();
            foreach (var rawId in cardIds.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                var cardId = rawId.StartsWith("CARD.", StringComparison.OrdinalIgnoreCase)
                    ? rawId[5..]
                    : rawId;
                CardModel canonical;
                try
                {
                    canonical = ModelDb.GetById<CardModel>(new ModelId("CARD", cardId));
                }
                catch
                {
                    missing.Add(cardId);
                    continue;
                }
                var card = canonical.ToMutable();
                var stats = new Dictionary<string, object?>();
                try
                {
                    foreach (var dynamicVar in card.DynamicVars.Values)
                        stats[dynamicVar.Name] = (int)dynamicVar.BaseValue;
                }
                catch { }
                var keywords = card.Keywords?
                    .Where(keyword => keyword != CardKeyword.None)
                    .Select(keyword => keyword.ToString())
                    .ToList();
                cards.Add(new Dictionary<string, object?>
                {
                    ["id"] = card.Id.Entry,
                    ["cost"] = card.EnergyCost?.GetResolved() ?? 0,
                    ["costs_x"] = card.EnergyCost?.CostsX ?? false,
                    ["star_cost"] = card.CurrentStarCost,
                    ["type"] = card.Type.ToString(),
                    ["rarity"] = card.Rarity.ToString(),
                    ["stats"] = stats.Count > 0 ? stats : null,
                    ["keywords"] = keywords?.Count > 0 ? keywords : null,
                    ["after_upgrade"] = GetUpgradedInfo(card),
                });
            }
            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_cards_result",
                ["success"] = true,
                ["cards"] = cards,
                ["missing"] = missing.Count > 0 ? missing : null,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectCards failed", ex);
        }
    }

    public Dictionary<string, object?> InspectTypeShape(string typeName)
    {
        try
        {
            EnsureModelDbInitialized();
            var asm = typeof(RunState).Assembly;
            var type = asm.GetType(typeName, throwOnError: false, ignoreCase: false);
            if (type == null)
                return Error($"Type not found: {typeName}");
            var fields = type
                .GetFields(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)
                .Select(f => new Dictionary<string, object?>
                {
                    ["name"] = f.Name,
                    ["static"] = f.IsStatic,
                    ["public"] = f.IsPublic,
                    ["field_type"] = f.FieldType.FullName,
                })
                .OrderBy(f => f["name"]?.ToString())
                .ToList();
            var properties = type
                .GetProperties(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)
                .Select(p => new Dictionary<string, object?>
                {
                    ["name"] = p.Name,
                    ["property_type"] = p.PropertyType.FullName,
                    ["can_read"] = p.CanRead,
                    ["can_write"] = p.CanWrite,
                    ["getter_public"] = p.GetMethod?.IsPublic,
                    ["setter_public"] = p.SetMethod?.IsPublic,
                })
                .OrderBy(p => p["name"]?.ToString())
                .ToList();
            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_type_shape_result",
                ["success"] = true,
                ["target_type"] = type.FullName,
                ["fields"] = fields,
                ["properties"] = properties,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectTypeShape failed", ex);
        }
    }

    public Dictionary<string, object?> InspectRelicPickingState()
    {
        try
        {
            var sync = RunManager.Instance?.TreasureRoomRelicSynchronizer;
            if (sync == null)
                return Error("TreasureRoomRelicSynchronizer is not available");
            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_relic_picking_state_result",
                ["success"] = true,
                ["state"] = RelicPickingStateSummary(sync),
                ["run_transition"] = DescribeRunTransitionState(),
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectRelicPickingState failed", ex);
        }
    }

    private Dictionary<string, object?> RelicPickingStateSummary(object sync)
    {
        var currentRelics = GetMember(sync, "CurrentRelics") as System.Collections.IEnumerable
            ?? GetMember(sync, "_currentRelics") as System.Collections.IEnumerable;
        var relics = (currentRelics?.Cast<object?>() ?? Enumerable.Empty<object?>())
            .Select((r, i) => new Dictionary<string, object?>
            {
                ["index"] = i,
                ["type"] = r?.GetType().FullName,
                ["id"] = GetMember(GetMember(r, "Id"), "Entry")?.ToString(),
                ["name"] = GetMember(r, "Name")?.ToString(),
            })
            .ToList();

        var votes = GetMember(sync, "_votes") as System.Collections.IDictionary;
        var voteSummaries = new List<Dictionary<string, object?>>();
        if (votes != null)
        {
            foreach (System.Collections.DictionaryEntry entry in votes)
            {
                voteSummaries.Add(new Dictionary<string, object?>
                {
                    ["key_type"] = entry.Key?.GetType().FullName,
                    ["value"] = entry.Value?.ToString(),
                    ["value_type"] = entry.Value?.GetType().FullName,
                });
            }
        }

        var playerVote = (_runState?.Players?.FirstOrDefault() is Player player)
            ? sync.GetType().GetMethod("GetPlayerVote", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                ?.Invoke(sync, new object?[] { player })
            : null;

        return new Dictionary<string, object?>
        {
            ["current_relic_count"] = relics.Count,
            ["current_relics"] = relics,
            ["predicted_vote"] = GetMember(sync, "_predictedVote")?.ToString(),
            ["player_vote"] = playerVote?.ToString(),
            ["player_vote_type"] = playerVote?.GetType().FullName,
            ["vote_count"] = votes?.Count ?? 0,
            ["votes"] = voteSummaries,
        };
    }

    private void DrainPendingRoomTransitions(string reason)
    {
        try
        {
            // Event/reward handlers can post follow-up continuations after the
            // action executor reports idle. Drain a few sync-context cycles at
            // room boundaries so sessions such as relic-picking are fully closed
            // before we enter the next map node.
            for (int i = 0; i < 5; i++)
            {
                _syncCtx.Pump();
                WaitForActionExecutor();
                _syncCtx.Pump();
                Thread.Sleep(1);
            }
        }
        catch (Exception ex)
        {
            Log($"DrainPendingRoomTransitions failed ({reason}): {ex.GetType().Name}: {ex.Message}");
            throw;
        }
    }

    private void ResolvePendingRelicPicking(string reason)
    {
        try
        {
            var sync = RunManager.Instance?.TreasureRoomRelicSynchronizer;
            if (sync == null || _runState == null || _runState.Players.Count == 0)
                return;
            var currentRelics = (GetMember(sync, "CurrentRelics") as System.Collections.IEnumerable
                ?? GetMember(sync, "_currentRelics") as System.Collections.IEnumerable)
                ?.Cast<object?>()
                .Where(r => r != null)
                .ToList() ?? new List<object?>();
            if (currentRelics.Count <= 0)
                return;
            if (currentRelics.Count != 1)
                throw new InvalidOperationException(
                    $"Pending relic choice has {currentRelics.Count} options; an explicit player decision is required");

            var player = _runState.Players[0];
            var playerVote = sync.GetType().GetMethod("GetPlayerVote", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                ?.Invoke(sync, new object?[] { player });
            if (GetMember(playerVote, "voteReceived") is bool voteReceived && voteReceived)
                return;

            Log($"Resolving pending relic picking ({reason}): {currentRelics.Count} relic(s), picking index 0");
            var picked = currentRelics[0] as RelicModel;
            if (picked == null)
                return;
            _autoResolvedRelicPicks += 1;
            _lastAutoResolvedRelicPick = new Dictionary<string, object?>
            {
                ["reason"] = reason,
                ["floor"] = _runState.ActFloor,
                ["relic_count"] = currentRelics.Count,
                ["picked_index"] = 0,
                ["picked_id"] = picked.Id.Entry,
                ["picked_type"] = picked?.GetType().FullName,
                ["awarded_by_synchronizer"] = false,
            };
            List<RelicPickingResult>? awarded = null;
            void OnRelicsAwarded(List<RelicPickingResult> results) => awarded = results;
            sync.RelicsAwarded += OnRelicsAwarded;
            try
            {
                sync.PickRelicLocally(0);
                WaitForActionExecutor();
                _syncCtx.Pump();
                DrainPendingRoomTransitions($"after_relic_pick:{reason}");
            }
            finally
            {
                sync.RelicsAwarded -= OnRelicsAwarded;
            }
            var localAward = awarded?.SingleOrDefault(result => result.player == player);
            if (localAward?.relic == null)
                throw new InvalidOperationException("Relic synchronizer did not award the local player");
            var relicCountBefore = player.Relics.Count;
            RelicCmd.Obtain(localAward.relic.ToMutable(), player, -1).GetAwaiter().GetResult();
            _syncCtx.Pump();
            WaitForActionExecutor();
            if (player.Relics.Count <= relicCountBefore)
                throw new InvalidOperationException("Native relic award did not reach the player");
            _lastAutoResolvedRelicPick["awarded_by_synchronizer"] = true;
            Log($"Resolved pending relic picking ({reason}): {System.Text.Json.JsonSerializer.Serialize(RelicPickingStateSummary(sync))}");
        }
        catch (Exception ex)
        {
            Log($"ResolvePendingRelicPicking failed ({reason}): {ex.GetType().Name}: {ex.Message}");
            throw;
        }
    }

    public Dictionary<string, object?> InspectEncounterState()
    {
        try
        {
            if (_runState?.CurrentRoom is not CombatRoom room)
                return Error("No combat room active");

            static IEnumerable<FieldInfo> GetFieldsAcrossHierarchy(Type? type)
            {
                for (var current = type; current != null; current = current.BaseType)
                {
                    foreach (var field in current.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                        yield return field;
                }
            }

            static List<Dictionary<string, object?>> Describe(object? obj, bool filtered = true)
            {
                if (obj == null)
                    return new List<Dictionary<string, object?>>();
                return GetFieldsAcrossHierarchy(obj.GetType())
                    .Where(f => !filtered ||
                                f.Name.Contains("state", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("move", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("turn", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("count", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("index", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("stock", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("enemy", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("creature", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("escape", StringComparison.OrdinalIgnoreCase))
                    .Select(f =>
                    {
                        object? raw = null;
                        try { raw = f.GetValue(obj); } catch { }
                        string? value;
                        int? count = null;
                        List<string>? items = null;
                        if (raw == null) value = null;
                        else if (raw is string || raw.GetType().IsPrimitive || raw.GetType().IsEnum || raw is decimal)
                            value = raw.ToString();
                        else
                        {
                            value = raw.GetType().FullName + ":" + raw;
                            if (raw is System.Collections.ICollection coll)
                            {
                                count = coll.Count;
                            }
                            if (raw is System.Collections.IEnumerable enumerable &&
                                (f.Name.Contains("enemy", StringComparison.OrdinalIgnoreCase) ||
                                 f.Name.Contains("creature", StringComparison.OrdinalIgnoreCase) ||
                                 f.Name.Contains("escape", StringComparison.OrdinalIgnoreCase) ||
                                 f.Name.Contains("monster", StringComparison.OrdinalIgnoreCase)))
                            {
                                items = new List<string>();
                                foreach (var entry in enumerable)
                                {
                                    if (entry == null)
                                    {
                                        items.Add("null");
                                        continue;
                                    }
                                    var hp = AnyMember(entry, "_currentHp") ?? AnyMember(entry, "<CurrentHp>k__BackingField");
                                    var block = AnyMember(entry, "_block") ?? AnyMember(entry, "<Block>k__BackingField");
                                    var monster = AnyMember(entry, "<Monster>k__BackingField") ?? AnyMember(entry, "Item1");
                                    var slot = AnyMember(entry, "Item2")?.ToString();
                                    var monsterId = AnyMember(monster, "<Id>k__BackingField")?.ToString()
                                        ?? AnyMember(monster, "Id")?.ToString()
                                        ?? AnyMember(entry, "Name")?.ToString()
                                        ?? entry.ToString();
                                    if (!string.IsNullOrWhiteSpace(slot))
                                        items.Add($"{monsterId}|slot={slot}|hp={hp}|block={block}");
                                    else
                                        items.Add($"{monsterId}|hp={hp}|block={block}");
                                }
                            }
                        }
                        return new Dictionary<string, object?>
                        {
                            ["name"] = f.Name,
                            ["type"] = f.FieldType.FullName,
                            ["value"] = value,
                            ["count"] = count,
                            ["items"] = items,
                        };
                    })
                    .ToList();
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_encounter_state_result",
                ["success"] = true,
                ["room_type"] = room.GetType().FullName,
                ["room_fields"] = Describe(room),
                ["room_all_fields"] = Describe(room, filtered: false),
                ["encounter_type"] = room.Encounter?.GetType().FullName,
                ["encounter_fields"] = Describe(room.Encounter),
                ["encounter_all_fields"] = Describe(room.Encounter, filtered: false),
                ["combat_state_fields"] = Describe(room.CombatState),
                ["combat_state_all_fields"] = Describe(room.CombatState, filtered: false),
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectEncounterState failed", ex);
        }
    }

    public Dictionary<string, object?> InspectPowerState()
    {
        try
        {
            if (_runState == null || CombatManager.Instance.DebugOnlyGetState() == null)
                return Error("No combat state available");

            static List<Dictionary<string, object?>> DescribeObject(object? obj)
            {
                if (obj == null)
                    return new List<Dictionary<string, object?>>();
                return obj.GetType()
                    .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .Select(f =>
                    {
                        object? raw = null;
                        try { raw = f.GetValue(obj); } catch { }
                        string? value;
                        if (raw == null) value = null;
                        else if (raw is string || raw.GetType().IsPrimitive || raw.GetType().IsEnum || raw is decimal)
                            value = raw.ToString();
                        else
                            value = raw.GetType().FullName + ":" + raw;
                        return new Dictionary<string, object?>
                        {
                            ["name"] = f.Name,
                            ["type"] = f.FieldType.FullName,
                            ["value"] = value,
                        };
                    })
                    .ToList();
            }

            static Dictionary<string, object?> DescribePower(string ownerLabel, PowerModel power)
            {
                object? internalData = null;
                try
                {
                    internalData = power.GetType()
                        .GetMethod("GetInternalData", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?
                        .Invoke(power, Array.Empty<object?>());
                }
                catch { }
                return new Dictionary<string, object?>
                {
                    ["owner"] = ownerLabel,
                    ["id"] = power.Id.Entry,
                    ["amount"] = power.Amount,
                    ["object_id"] = RuntimeHelpers.GetHashCode(power),
                    ["fields"] = DescribeObject(power),
                    ["internal_data_type"] = internalData?.GetType().FullName,
                    ["internal_data_fields"] = DescribeObject(internalData),
                };
            }

            var rows = new List<object?>();
            var player = _runState.Players.FirstOrDefault();
            if (player?.Creature != null)
            {
                foreach (var power in player.Creature.Powers ?? Enumerable.Empty<PowerModel>())
                    rows.Add(DescribePower("player", power));
            }

            foreach (var enemy in CombatManager.Instance.DebugOnlyGetState()?.Enemies ?? Enumerable.Empty<Creature>())
            {
                var ownerLabel = enemy.Monster?.Id.Entry ?? enemy.Name ?? "enemy";
                foreach (var power in enemy.Powers ?? Enumerable.Empty<PowerModel>())
                    rows.Add(DescribePower(ownerLabel, power));
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_power_state_result",
                ["success"] = true,
                ["powers"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectPowerState failed", ex);
        }
    }

    public Dictionary<string, object?> InspectRelicState()
    {
        try
        {
            if (_runState == null)
                return Error("No run state available");

            static List<Dictionary<string, object?>> DescribeFields(object? obj)
            {
                if (obj == null)
                    return new List<Dictionary<string, object?>>();
                return obj.GetType()
                    .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .Select(f =>
                    {
                        object? raw = null;
                        try { raw = f.GetValue(obj); } catch { }
                        string? value;
                        if (raw == null) value = null;
                        else if (raw is string || raw.GetType().IsPrimitive || raw.GetType().IsEnum || raw is decimal)
                            value = raw.ToString();
                        else
                            value = raw.GetType().FullName + ":" + raw;
                        return new Dictionary<string, object?>
                        {
                            ["name"] = f.Name,
                            ["type"] = f.FieldType.FullName,
                            ["value"] = value,
                        };
                    })
                    .ToList();
            }

            var rows = new List<object?>();
            var player = _runState.Players.FirstOrDefault();
            foreach (var relic in player?.Relics ?? Enumerable.Empty<RelicModel>())
            {
                rows.Add(new Dictionary<string, object?>
                {
                    ["id"] = relic.Id.Entry,
                    ["type"] = relic.GetType().FullName,
                    ["fields"] = DescribeFields(relic),
                });
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_relic_state_result",
                ["success"] = true,
                ["relics"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectRelicState failed", ex);
        }
    }

    // Deep-dump every primitive/enum/string LEAF field of the live combat
    // runtime (PlayerCombatState, player Creature, CombatState), keyed by full
    // path. Unlike InspectRngState's DescribeObject (which skips primitives and
    // only recurses non-System types), this captures the int/bool counters that
    // hold per-combat derived state — e.g. an exhaust/cards-played tally that an
    // attack like ASHEN_STRIKE reads. Diffing this dump clean-vs-churn localizes
    // in_place restore leaks that are invisible in the serialized snapshot.
    public Dictionary<string, object?> InspectCombatRuntime(int maxDepth = 4)
    {
        try
        {
            if (_runState == null) return Error("No run state available");
            var player = _runState.Players.FirstOrDefault();
            if (player == null) return Error("No player");

            var leaves = new Dictionary<string, object?>();

            void Walk(string path, object? obj, int depth, HashSet<object> seen)
            {
                if (obj == null || depth > maxDepth) return;
                var t = obj.GetType();
                if (t.IsPrimitive || t.IsEnum || obj is string || obj is decimal)
                {
                    leaves[path] = obj is Enum ? obj.ToString() : obj;
                    return;
                }
                if (!seen.Add(obj)) return;
                if (obj is System.Collections.IEnumerable en && !(obj is string))
                {
                    int i = 0;
                    foreach (var item in en)
                    {
                        Walk($"{path}[{i}]", item, depth + 1, seen);
                        if (++i > 256) break;
                    }
                    return;
                }
                var fn = t.FullName ?? "";
                if (fn.StartsWith("System.", StringComparison.Ordinal) ||
                    fn.StartsWith("Microsoft.", StringComparison.Ordinal) ||
                    fn.StartsWith("Godot", StringComparison.Ordinal))
                    return;
                foreach (var f in t.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                {
                    // Skip player-global metadata (unlock state, card library) — it
                    // is huge and combat-irrelevant, and would exhaust the budget
                    // before reaching per-combat counters.
                    if (f.Name == "_player" || f.Name.Contains("UnlockState") ||
                        f.Name.Contains("CardLibrary") || f.Name.Contains("_cardLibrary"))
                        continue;
                    object? v = null;
                    try { v = f.GetValue(obj); } catch { continue; }
                    Walk($"{path}.{f.Name}", v, depth + 1, seen);
                }
            }

            Walk("pcs", player.PlayerCombatState, 0, new HashSet<object>(ReferenceEqualityComparer.Instance));
            Walk("creature", player.Creature, 0, new HashSet<object>(ReferenceEqualityComparer.Instance));
            Walk("combat", CombatManager.Instance.DebugOnlyGetState(), 0, new HashSet<object>(ReferenceEqualityComparer.Instance));

            // Explicit registry counts: _allCards is the CombatState master card
            // list; if it grows across restores while visible piles stay stable,
            // restore is orphaning card instances into it (an ASHEN_STRIKE-style
            // exhaust/card count would then inflate).
            try
            {
                var cs = CombatManager.Instance.DebugOnlyGetState();
                if (cs != null && AnyMember(cs, "_allCards") is System.Collections.IEnumerable all)
                {
                    int n = 0;
                    var zoneTally = new Dictionary<string, int>(StringComparer.Ordinal);
                    foreach (var item in all)
                    {
                        n++;
                        if (item is CardModel cm)
                        {
                            // Dump any per-card pile/zone/location field: a reused
                            // pooled orphan may carry a stale "Exhaust" zone enum
                            // even though the CardPile lists are correct, which an
                            // ASHEN_STRIKE-style exhaust count (iterating _allCards
                            // by zone) would read.
                            foreach (var f in cm.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                            {
                                var fn2 = f.Name.ToLowerInvariant();
                                if (!(fn2.Contains("pile") || fn2.Contains("zone") || fn2.Contains("location") || fn2.Contains("container"))) continue;
                                try
                                {
                                    var fv = f.GetValue(cm);
                                    var key = $"{cm.Id.Entry}.{f.Name}={(fv == null ? "null" : (fv.GetType().IsEnum || fv.GetType().IsPrimitive ? fv.ToString() : fv.GetType().Name))}";
                                    zoneTally[key] = zoneTally.GetValueOrDefault(key) + 1;
                                }
                                catch { }
                            }
                        }
                    }
                    leaves["combat._allCards.Count"] = n;
                    foreach (var (k, cnt) in zoneTally)
                        leaves[$"allcard_zone.{k}"] = cnt;
                }
            }
            catch { }

            // CombatManager.Instance.History entry count — per-turn event log
            // (CardExhausted/CardPlayed/...) that cards like FORGOTTEN_RITUAL read
            // ("was a card exhausted this turn"). If it grows across restores, the
            // in_place restore is leaking stale combat events.
            try
            {
                var hist = CombatManager.Instance?.History;
                if (hist != null)
                {
                    var entriesProp = hist.GetType().GetProperty("Entries", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    if (entriesProp?.GetValue(hist) is System.Collections.IEnumerable en2)
                    {
                        int hn = 0; foreach (var _ in en2) hn++;
                        leaves["combat.History.Entries.Count"] = hn;
                    }
                }
            }
            catch { }

            // Broad multicast-delegate handler tally: walk the combat object graph
            // and record every delegate field's invocation-list length by path. A
            // duplicated damage-modifier subscription (revealed by ANY attack
            // inflating after churn, not just exhaust-scaling ASHEN) shows up here
            // as a handler count that grows across restores.
            try
            {
                var dseen = new HashSet<object>(ReferenceEqualityComparer.Instance);
                void WalkDel(string path, object? obj, int depth)
                {
                    if (obj == null || depth > 5) return;
                    var t = obj.GetType();
                    if (t.IsPrimitive || t.IsEnum || obj is string || obj is decimal) return;
                    if (!dseen.Add(obj)) return;
                    var fnn = t.FullName ?? "";
                    if (fnn.StartsWith("System.", StringComparison.Ordinal) ||
                        fnn.StartsWith("Microsoft.", StringComparison.Ordinal) ||
                        fnn.StartsWith("Godot", StringComparison.Ordinal)) return;
                    if (obj is System.Collections.IEnumerable en0 && !(obj is string))
                    {
                        int i = 0;
                        foreach (var it in en0) { WalkDel($"{path}[{i}]", it, depth + 1); if (++i > 64) break; }
                        return;
                    }
                    foreach (var f in t.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                    {
                        if (f.Name == "_player" || f.Name.Contains("UnlockState") || f.Name.Contains("CardLibrary")) continue;
                        object? v = null;
                        try { v = f.GetValue(obj); } catch { continue; }
                        if (v is Delegate del)
                            leaves[$"del.{path}.{f.Name}"] = del.GetInvocationList().Length;
                        else
                            WalkDel($"{path}.{f.Name}", v, depth + 1);
                    }
                }
                WalkDel("pcs", player.PlayerCombatState, 0);
                WalkDel("creature", player.Creature, 0);
                WalkDel("combat", CombatManager.Instance.DebugOnlyGetState(), 0);
            }
            catch { }

            // Engine-side pile card counts (true .Cards.Count, not the rendered
            // search-state pile). A pile whose internal list holds orphaned cards
            // a restore failed to clear shows up here even when the rendered pile
            // is empty.
            try
            {
                foreach (var pile in player.PlayerCombatState?.AllPiles ?? Enumerable.Empty<CardPile>())
                {
                    leaves[$"pile.{pile.Type}.Count"] = pile.Cards?.Count ?? 0;
                    // Multicast-delegate invocation-list lengths on the pile's
                    // change events: a reused worker that re-subscribes each
                    // restore without unsubscribing accumulates handlers here,
                    // and each fires the DB re-ID / damage recalc again.
                    foreach (var f in pile.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                    {
                        try
                        {
                            if (f.GetValue(pile) is Delegate del)
                            {
                                var il = del.GetInvocationList();
                                leaves[$"pileEvt.{pile.Type}.{f.Name}.handlers"] = il.Length;
                                if (pile.Type.ToString() == "Exhaust")
                                {
                                    var tally = new Dictionary<string, int>(StringComparer.Ordinal);
                                    foreach (var h in il)
                                    {
                                        var key = $"{h.Method.DeclaringType?.Name}.{h.Method.Name}";
                                        tally[key] = tally.GetValueOrDefault(key) + 1;
                                    }
                                    foreach (var (mk, mc) in tally)
                                        leaves[$"exhaustEvt.{mk}"] = mc;
                                }
                            }
                        }
                        catch { }
                    }
                }
            }
            catch { }

            // Hand-card DynamicVars (e.g. ASHEN_STRIKE.ExtraDamage). A pooled
            // card instance reused across restores may carry a stale recalculated
            // var that the restore did not reset — invisible in pile state but it
            // drives actual damage.
            try
            {
                int ci = 0;
                foreach (var card in player.PlayerCombatState?.Hand?.Cards ?? Enumerable.Empty<CardModel>())
                {
                    var dvs = card.DynamicVars?.Values;
                    if (dvs == null) { ci++; continue; }
                    foreach (var dv in dvs)
                    {
                        // Dump EVERY instance field of the DynamicVar object, not
                        // just BaseValue — a recalculated/cached resolved value
                        // (the number that actually drives damage) may live in a
                        // separate field that an in_place restore leaves stale.
                        try { leaves[$"hand[{ci}].{card.Id.Entry}.{dv.Name}.base"] = (int)dv.BaseValue; } catch { }
                        object dvo = dv;
                        foreach (var f in dvo.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                        {
                            try
                            {
                                var fv = f.GetValue(dvo);
                                if (fv == null) continue;
                                if (fv.GetType().IsPrimitive || fv is string || fv.GetType().IsEnum)
                                    leaves[$"hand[{ci}].{card.Id.Entry}.{dv.Name}.f.{f.Name}"] = fv is Enum ? fv.ToString() : fv;
                                else if (fv is System.Collections.ICollection col)
                                    leaves[$"hand[{ci}].{card.Id.Entry}.{dv.Name}.f.{f.Name}.Count"] = col.Count;
                                else if (fv is System.Collections.IEnumerable en && !(fv is string))
                                {
                                    int n = 0; foreach (var _ in en) { if (++n > 500) break; }
                                    leaves[$"hand[{ci}].{card.Id.Entry}.{dv.Name}.f.{f.Name}.Count"] = n;
                                }
                            }
                            catch { }
                        }
                    }
                    ci++;
                }
            }
            catch { }

            // NetCombatCardDb singleton card-registry count. RestoreCombatCardDb
            // clears+rebuilds it each restore; if it instead grows, a card-count
            // read (ASHEN_STRIKE exhaust scaling) inflates across reuse.
            try
            {
                var dbType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb");
                var inst = dbType?.GetProperty("Instance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)?.GetValue(null);
                if (inst != null)
                {
                    foreach (var f in dbType!.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                    {
                        try
                        {
                            var v = f.GetValue(inst);
                            if (v is System.Collections.ICollection col)
                                leaves[$"cardDb.{f.Name}.Count"] = col.Count;
                            else if (v is System.Collections.IEnumerable en && !(v is string))
                            {
                                int n = 0; foreach (var _ in en) { if (++n > 5000) break; }
                                leaves[$"cardDb.{f.Name}.Count"] = n;
                            }
                            else if (v != null && (v.GetType().IsPrimitive))
                                leaves[$"cardDb.{f.Name}"] = v;
                            if (f.Name == "_subscriptions" && v != null)
                            {
                                leaves["cardDb._subscriptions.fieldType"] = v.GetType().FullName;
                                if (v is System.Collections.IEnumerable se)
                                {
                                    foreach (var it in se)
                                    {
                                        if (it == null) continue;
                                        leaves["cardDb._subscriptions.elemType"] = it.GetType().FullName;
                                        leaves["cardDb._subscriptions.elemDisposable"] = it is IDisposable;
                                        foreach (var sf in it.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                                        {
                                            object? sfv = null;
                                            try { sfv = sf.GetValue(it); } catch { }
                                            leaves[$"cardDb._subscriptions.elem.{sf.Name}.type"] = sf.FieldType.FullName;
                                            if (sfv != null && (sfv.GetType().IsPrimitive || sfv.GetType().IsEnum || sfv is string))
                                                leaves[$"cardDb._subscriptions.elem.{sf.Name}.val"] = sfv.ToString();
                                        }
                                        break;
                                    }
                                }
                            }
                        }
                        catch { }
                    }
                }
            }
            catch { }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_combat_runtime_result",
                ["success"] = true,
                ["leaf_count"] = leaves.Count,
                ["leaves"] = leaves,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectCombatRuntime failed", ex);
        }
    }

    public Dictionary<string, object?> InspectCombatHistory()
    {
        try
        {
            var history = CombatManager.Instance?.History;
            if (history == null)
                return Error("Combat history unavailable");

            var entriesProperty = history.GetType().GetProperty(
                "Entries", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (entriesProperty?.GetValue(history) is not System.Collections.IEnumerable entries)
                return Error("Combat history entries unavailable");

            var rows = new List<object?>();
            foreach (var entry in entries)
            {
                if (entry == null)
                    continue;
                var fields = new List<Dictionary<string, object?>>();
                for (var type = entry.GetType(); type != null; type = type.BaseType)
                {
                    foreach (var field in type.GetFields(
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic |
                        BindingFlags.DeclaredOnly))
                    {
                        object? value = null;
                        try { value = field.GetValue(entry); } catch { }
                        var scalar = value == null || value is string || value is decimal ||
                            value.GetType().IsPrimitive || value.GetType().IsEnum;
                        fields.Add(new Dictionary<string, object?>
                        {
                            ["declaring_type"] = type.FullName,
                            ["name"] = field.Name,
                            ["field_type"] = field.FieldType.FullName,
                            ["value"] = scalar ? (value is Enum ? value.ToString() : value) : null,
                            ["reference_type"] = scalar ? null : value?.GetType().FullName,
                            ["reference_id"] = scalar || value == null
                                ? null
                                : System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(value),
                        });
                    }
                }
                rows.Add(new Dictionary<string, object?>
                {
                    ["type"] = entry.GetType().FullName,
                    ["fields"] = fields,
                });
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_combat_history_result",
                ["success"] = true,
                ["entry_count"] = rows.Count,
                ["entries"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectCombatHistory failed", ex);
        }
    }

    // Scan STATIC int/long/bool/collection-count fields across all combat-ish
    // types in the game assembly. The ASHEN_STRIKE in_place residue is invisible
    // to every instance-graph walk, so the leaking per-play counter must live in
    // a static field (a singleton manager, an action-history, a static tally).
    // Diff this clean-vs-churn to find a static that increments per card played
    // and is not reset by in_place restore.
    public Dictionary<string, object?> InspectStaticCounters(string? filter = null)
    {
        try
        {
            var asm = typeof(PlayCardAction).Assembly;
            var leaves = new Dictionary<string, object?>();
            Type[] allTypes;
            try { allTypes = asm.GetTypes(); }
            catch (ReflectionTypeLoadException rtle) { allTypes = rtle.Types.Where(t => t != null).ToArray()!; }
            leaves["__scanned_types"] = allTypes.Length;
            foreach (var t in allTypes)
            {
                if (t == null) continue;
                var ns = t.Namespace ?? "";
                if (!ns.StartsWith("MegaCrit.Sts2", StringComparison.Ordinal)) continue;
                if (filter != null && t.FullName?.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                FieldInfo[] fields;
                try { fields = t.GetFields(BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic); }
                catch { continue; }
                foreach (var f in fields)
                {
                    var ft = f.FieldType;
                    try
                    {
                        object? v = f.GetValue(null);
                        if (v == null) continue;
                        if (ft == typeof(int) || ft == typeof(long) || ft == typeof(bool) ||
                            ft == typeof(uint) || ft == typeof(short) || ft == typeof(byte))
                            leaves[$"{t.FullName}.{f.Name}"] = v;
                        else if (v is System.Collections.ICollection col)
                            leaves[$"{t.FullName}.{f.Name}.Count"] = col.Count;
                        else if (v is System.Collections.IEnumerable en && !(v is string))
                        {
                            int n = 0; foreach (var _ in en) { if (++n > 1000) break; }
                            leaves[$"{t.FullName}.{f.Name}.Count"] = n;
                        }
                    }
                    catch { }
                }
            }
            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_static_counters_result",
                ["success"] = true,
                ["leaf_count"] = leaves.Count,
                ["leaves"] = leaves,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectStaticCounters failed", ex);
        }
    }

    public Dictionary<string, object?> InspectHookListeners()
    {
        try
        {
            var combatState = CombatManager.Instance.DebugOnlyGetState();
            if (_runState == null || combatState == null)
                return Error("No combat state available");

            static List<Dictionary<string, object?>> DescribeObject(object? obj)
            {
                if (obj == null)
                    return new List<Dictionary<string, object?>>();
                var fields = new List<FieldInfo>();
                for (var current = obj.GetType(); current != null; current = current.BaseType)
                    fields.AddRange(current.GetFields(BindingFlags.Instance | BindingFlags.Public
                        | BindingFlags.NonPublic | BindingFlags.DeclaredOnly));
                return fields
                    .Select(f =>
                    {
                        object? raw = null;
                        try { raw = f.GetValue(obj); } catch { }
                        string? value;
                        if (raw == null) value = null;
                        else if (raw is string || raw.GetType().IsPrimitive || raw.GetType().IsEnum || raw is decimal)
                            value = raw.ToString();
                        else
                            value = raw.GetType().FullName + ":" + raw;
                        return new Dictionary<string, object?>
                        {
                            ["name"] = f.Name,
                            ["type"] = f.FieldType.FullName,
                            ["value"] = value,
                        };
                    })
                    .ToList();
            }

            var method = combatState.GetType().GetMethod("IterateHookListeners", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (method == null)
                return Error("IterateHookListeners not found");

            var raw = method.Invoke(combatState, Array.Empty<object?>());
            if (raw is not System.Collections.IEnumerable listeners)
                return Error("IterateHookListeners did not return IEnumerable");

            var rows = new List<object?>();
            foreach (var listener in listeners)
            {
                rows.Add(new Dictionary<string, object?>
                {
                    ["type"] = listener?.GetType().FullName,
                    ["fields"] = DescribeObject(listener),
                });
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_hook_listeners_result",
                ["success"] = true,
                ["listener_count"] = rows.Count,
                ["listeners"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectHookListeners failed", ex);
        }
    }

    public Dictionary<string, object?> InspectEnemyGraph(int enemyIndex = 0, int depth = 3)
    {
        try
        {
            var combatState = CombatManager.Instance.DebugOnlyGetState();
            if (combatState?.Enemies == null || enemyIndex < 0 || enemyIndex >= combatState.Enemies.Count)
                return Error($"Enemy index {enemyIndex} unavailable");

            static bool IsScalarLike(object obj)
            {
                var t = obj.GetType();
                return obj is string || t.IsPrimitive || t.IsEnum || obj is decimal;
            }

            static IEnumerable<FieldInfo> GetFieldsAcrossHierarchy(Type? type)
            {
                for (var current = type; current != null; current = current.BaseType)
                {
                    foreach (var field in current.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                        yield return field;
                }
            }

            static bool ShouldTraverse(Type type)
            {
                var full = type.FullName ?? string.Empty;
                return !full.StartsWith("System.", StringComparison.Ordinal) &&
                       !full.StartsWith("Microsoft.", StringComparison.Ordinal);
            }

            var root = combatState.Enemies[enemyIndex];
            var rows = new List<object?>();
            var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);

            void Walk(object? obj, string path, int remainingDepth)
            {
                if (obj == null)
                {
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["path"] = path,
                        ["kind"] = "null",
                    });
                    return;
                }

                var type = obj.GetType();
                if (IsScalarLike(obj))
                {
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["path"] = path,
                        ["kind"] = "scalar",
                        ["type"] = type.FullName,
                        ["value"] = obj.ToString(),
                    });
                    return;
                }

                if (!seen.Add(obj))
                {
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["path"] = path,
                        ["kind"] = "ref",
                        ["type"] = type.FullName,
                        ["object_id"] = RuntimeHelpers.GetHashCode(obj),
                        ["cycle"] = true,
                    });
                    return;
                }

                rows.Add(new Dictionary<string, object?>
                {
                    ["path"] = path,
                    ["kind"] = "object",
                    ["type"] = type.FullName,
                    ["object_id"] = RuntimeHelpers.GetHashCode(obj),
                });

                if (remainingDepth <= 0 || !ShouldTraverse(type))
                    return;

                foreach (var field in GetFieldsAcrossHierarchy(type))
                {
                    object? value = null;
                    try { value = field.GetValue(obj); } catch { }
                    var childPath = $"{path}.{field.DeclaringType?.Name}.{field.Name}";
                    if (value == null || IsScalarLike(value))
                    {
                        Walk(value, childPath, remainingDepth - 1);
                    }
                    else if (value is System.Collections.IEnumerable enumerable && value is not string)
                    {
                        var index = 0;
                        foreach (var item in enumerable)
                        {
                            Walk(item, $"{childPath}[{index}]", remainingDepth - 1);
                            index++;
                            if (index >= 16)
                                break;
                        }
                    }
                    else
                    {
                        Walk(value, childPath, remainingDepth - 1);
                    }
                }
            }

            Walk(root, $"enemy[{enemyIndex}]", Math.Max(1, depth));

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_enemy_graph_result",
                ["success"] = true,
                ["enemy_index"] = enemyIndex,
                ["rows"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectEnemyGraph failed", ex);
        }
    }

    public Dictionary<string, object?> InspectCardCostRuntime()
    {
        try
        {
            var player = _runState?.Players?.FirstOrDefault();
            var card = player?.PlayerCombatState?.Hand?.Cards?.FirstOrDefault();
            if (card == null)
                return Error("No combat card available");

            static IEnumerable<FieldInfo> GetFieldsAcrossHierarchy(Type? type)
            {
                for (var current = type; current != null; current = current.BaseType)
                {
                    foreach (var field in current.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                        yield return field;
                }
            }

            static IEnumerable<MethodInfo> GetMethodsAcrossHierarchy(Type? type)
            {
                for (var current = type; current != null; current = current.BaseType)
                {
                    foreach (var method in current.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                        yield return method;
                }
            }

            var energyField = GetFieldsAcrossHierarchy(card.GetType()).FirstOrDefault(f => f.Name == "_energyCost");
            var energyValue = energyField?.GetValue(card);
            var energyType = energyField?.FieldType ?? energyValue?.GetType();
            var methods = GetMethodsAcrossHierarchy(card.GetType())
                .Where(m => m.Name.Contains("cost", StringComparison.OrdinalIgnoreCase) ||
                            m.Name.Contains("energy", StringComparison.OrdinalIgnoreCase) ||
                            m.Name.Contains("combat", StringComparison.OrdinalIgnoreCase) ||
                            m.Name.Contains("resolve", StringComparison.OrdinalIgnoreCase))
                .Select(m => new Dictionary<string, object?>
                {
                    ["name"] = m.Name,
                    ["parameters"] = m.GetParameters().Select(p => $"{p.ParameterType.Name} {p.Name}").ToList(),
                })
                .ToList();
            var ctors = energyType?.GetConstructors(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                .Select(c => new Dictionary<string, object?>
                {
                    ["parameters"] = c.GetParameters().Select(p => $"{p.ParameterType.Name} {p.Name}").ToList(),
                })
                .ToList();

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_card_cost_runtime_result",
                ["success"] = true,
                ["card_type"] = card.GetType().FullName,
                ["energy_field_type"] = energyField?.FieldType.FullName,
                ["energy_value_type"] = energyValue?.GetType().FullName,
                ["cost_related_fields"] = GetFieldsAcrossHierarchy(card.GetType())
                    .Where(f => f.Name.Contains("cost", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("energy", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("star", StringComparison.OrdinalIgnoreCase))
                    .Select(f => new Dictionary<string, object?>
                    {
                        ["name"] = f.Name,
                        ["type"] = f.FieldType.FullName,
                        ["value"] = f.GetValue(card)?.ToString(),
                    })
                    .ToList(),
                ["energy_fields"] = energyValue?.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .Select(f => new Dictionary<string, object?>
                    {
                        ["name"] = f.Name,
                        ["type"] = f.FieldType.FullName,
                        ["value"] = f.GetValue(energyValue)?.ToString(),
                    })
                    .ToList(),
                ["constructors"] = ctors,
                ["methods"] = methods,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectCardCostRuntime failed", ex);
        }
    }

    public Dictionary<string, object?> InspectAllCardRuntime()
    {
        try
        {
            static IEnumerable<FieldInfo> GetFieldsAcrossHierarchy(Type? type)
            {
                for (var current = type; current != null; current = current.BaseType)
                {
                    foreach (var field in current.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                        yield return field;
                }
            }

            Dictionary<string, object?> DumpCard(CardModel card, string location, int index)
            {
                var fields = GetFieldsAcrossHierarchy(card.GetType()).ToList();
                var energyField = fields.FirstOrDefault(f => f.Name == "_energyCost");
                var energyValue = energyField?.GetValue(card);
                var costFields = fields
                    .Where(f => f.Name.Contains("cost", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("energy", StringComparison.OrdinalIgnoreCase) ||
                                f.Name.Contains("star", StringComparison.OrdinalIgnoreCase))
                    .Select(f => new Dictionary<string, object?>
                    {
                        ["declaring_type"] = f.DeclaringType?.FullName,
                        ["name"] = f.Name,
                        ["type"] = f.FieldType.FullName,
                        ["value"] = f.GetValue(card)?.ToString(),
                    })
                    .ToList();
                var energyFields = energyValue == null
                    ? new List<Dictionary<string, object?>>()
                    : GetFieldsAcrossHierarchy(energyValue.GetType())
                        .Select(f => new Dictionary<string, object?>
                        {
                            ["declaring_type"] = f.DeclaringType?.FullName,
                            ["name"] = f.Name,
                            ["type"] = f.FieldType.FullName,
                            ["value"] = f.GetValue(energyValue)?.ToString(),
                        })
                        .ToList();

                return new Dictionary<string, object?>
                {
                    ["location"] = location,
                    ["index"] = index,
                    ["card_id"] = card.Id.Entry,
                    ["upgrade"] = Convert.ToInt32(GetMember(card, "CurrentUpgradeLevel") ?? 0),
                    ["object_id"] = RuntimeHelpers.GetHashCode(card),
                    ["energy_type"] = energyValue?.GetType().FullName,
                    ["cost_fields"] = costFields,
                    ["energy_fields"] = energyFields,
                };
            }

            var combatState = CombatManager.Instance.DebugOnlyGetState();
            var player = _runState?.Players?.FirstOrDefault();
            var pcs = player?.PlayerCombatState;
            if (combatState == null || pcs == null)
                return Error("No active combat state");

            var rows = new List<Dictionary<string, object?>>();
            foreach (var pile in pcs.AllPiles)
            {
                for (int i = 0; i < pile.Cards.Count; i++)
                    rows.Add(DumpCard(pile.Cards[i], $"pile:{pile.Type}", i));
            }

            var allCards = AnyMember(combatState, "_allCards") as System.Collections.IEnumerable;
            if (allCards != null)
            {
                var idx = 0;
                foreach (var item in allCards)
                {
                    if (item is CardModel card)
                        rows.Add(DumpCard(card, "combat:_allCards", idx));
                    idx++;
                }
            }

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_all_card_runtime_result",
                ["success"] = true,
                ["cards"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectAllCardRuntime failed", ex);
        }
    }

    public Dictionary<string, object?> InspectRngGraph(string rngName, int depth = 4)
    {
        try
        {
            static IEnumerable<FieldInfo> GetFieldsAcrossHierarchy(Type? type)
            {
                for (var current = type; current != null; current = current.BaseType)
                {
                    foreach (var field in current.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                        yield return field;
                }
            }

            var rngSet = _runState?.Rng;
            if (rngSet == null)
                return Error("No run RNG set");

            var dict = rngSet.GetType()
                .GetField("_rngs", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                ?.GetValue(rngSet) as System.Collections.IDictionary;
            if (dict == null)
                return Error("Run RNG dictionary unavailable");

            object? target = null;
            foreach (System.Collections.DictionaryEntry entry in dict)
            {
                if (string.Equals(entry.Key?.ToString(), rngName, StringComparison.Ordinal))
                {
                    target = entry.Value;
                    break;
                }
            }

            if (target == null)
                return Error($"RNG '{rngName}' not found");

            static bool IsScalarLike(object value)
            {
                var type = value.GetType();
                return type.IsPrimitive || value is string || value is decimal || value is Enum || value is ModelId;
            }

            static bool ShouldTraverse(Type type)
            {
                return !type.IsPrimitive && type != typeof(string) && type != typeof(decimal);
            }

            var rows = new List<Dictionary<string, object?>>();
            var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);

            void Walk(object? obj, string path, int remainingDepth)
            {
                if (obj == null)
                {
                    rows.Add(new Dictionary<string, object?> { ["path"] = path, ["kind"] = "null" });
                    return;
                }

                var type = obj.GetType();
                if (IsScalarLike(obj))
                {
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["path"] = path,
                        ["kind"] = "scalar",
                        ["type"] = type.FullName,
                        ["value"] = obj.ToString(),
                    });
                    return;
                }

                if (!seen.Add(obj))
                {
                    rows.Add(new Dictionary<string, object?>
                    {
                        ["path"] = path,
                        ["kind"] = "ref",
                        ["type"] = type.FullName,
                        ["object_id"] = RuntimeHelpers.GetHashCode(obj),
                        ["cycle"] = true,
                    });
                    return;
                }

                rows.Add(new Dictionary<string, object?>
                {
                    ["path"] = path,
                    ["kind"] = "object",
                    ["type"] = type.FullName,
                    ["object_id"] = RuntimeHelpers.GetHashCode(obj),
                });

                if (remainingDepth <= 0 || !ShouldTraverse(type))
                    return;

                foreach (var field in GetFieldsAcrossHierarchy(type))
                {
                    object? value = null;
                    try { value = field.GetValue(obj); } catch { }
                    Walk(value, $"{path}.{field.DeclaringType?.Name}.{field.Name}", remainingDepth - 1);
                }
            }

            Walk(target, $"rng[{rngName}]", Math.Max(1, depth));

            return new Dictionary<string, object?>
            {
                ["type"] = "inspect_rng_graph_result",
                ["success"] = true,
                ["rng_name"] = rngName,
                ["rows"] = rows,
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("InspectRngGraph failed", ex);
        }
    }

    private static object? SafeEvaluateBranchWeight(object? randomBranchState, object? branchStateWeight, Creature owner)
    {
        try
        {
            var method = randomBranchState?.GetType().GetMethod("GetStateWeight",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (method == null)
                return null;
            return method.Invoke(randomBranchState, new[] { branchStateWeight, owner });
        }
        catch
        {
            return null;
        }
    }

    private Dictionary<string, object?> BuildLiveCombatSearchState()
    {
        var state = new Dictionary<string, object?>
        {
            ["schema_version"] = "search_state_v1",
            ["success"] = false,
        };

        var player = _runState?.Players?.FirstOrDefault();
        var pcs = player?.PlayerCombatState;
        var combatState = CombatManager.Instance.DebugOnlyGetState();
        if (player == null || pcs == null || combatState == null || !IsPlayPhase())
        {
            state["message"] = "Current state is not combat_play";
            return state;
        }

        List<object?> NormalizeCardPile(System.Collections.IEnumerable? cards, bool sortForVisibility = false)
        {
            var normalized = (cards?.Cast<object?>() ?? Enumerable.Empty<object?>())
                .Where(c => c != null)
                .Select(c =>
                {
                    var keywordsObj = GetMember(c, "Keywords") as System.Collections.IEnumerable;
                    var keywords = keywordsObj?.Cast<object?>()
                        .Where(k => k != null && !string.Equals(k.ToString(), CardKeyword.None.ToString(), StringComparison.Ordinal))
                        .Select(k => k?.ToString())
                        .Where(k => !string.IsNullOrWhiteSpace(k))
                        .Cast<object?>()
                        .ToList();

                    string? affliction = null;
                    int? afflictionCount = null;
                    try
                    {
                        var afflictionObj = GetMember(c, "Affliction");
                        if (afflictionObj != null)
                        {
                            affliction = EntryOf(afflictionObj);
                            var amountObj = GetMember(afflictionObj, "Amount");
                            if (amountObj != null)
                                afflictionCount = Convert.ToInt32(amountObj);
                        }
                    }
                    catch { }

                    return new Dictionary<string, object?>
                    {
                        ["card_id"] = EntryOf(c),
                        ["upgrade"] = Convert.ToInt32(GetMember(c, "CurrentUpgradeLevel") ?? 0),
                        ["current_cost"] = GetMember(GetMember(c, "EnergyCost"), "ResolvedValue") ?? GetMember(GetMember(c, "EnergyCost"), "Value"),
                        ["display_cost"] = c is CardModel cardModel ? cardModel.EnergyCost?.GetWithModifiers((CostModifiers)(-1)) : null,
                        ["display_costs_x"] = c is CardModel xCard ? xCard.EnergyCost?.CostsX : null,
                        ["keywords"] = keywords?.Count > 0 ? keywords : null,
                        ["affliction"] = affliction,
                        ["affliction_count"] = afflictionCount,
                    };
                })
                .Cast<object?>()
                .ToList();

            if (sortForVisibility)
            {
                normalized = normalized
                    .OfType<Dictionary<string, object?>>()
                    .OrderBy(c => c.GetValueOrDefault("card_id")?.ToString() ?? "")
                    .ThenBy(c => Convert.ToInt32(c.GetValueOrDefault("upgrade") ?? 0))
                    .ThenBy(c => Convert.ToInt32(c.GetValueOrDefault("current_cost") ?? 0))
                    .Cast<object?>()
                    .ToList();
            }

            return normalized;
        }

        List<object?> NormalizePowers(System.Collections.IEnumerable? powers)
        {
            if (powers == null) return new List<object?>();
            return powers.Cast<object?>().Where(p => p != null).Select(p => new Dictionary<string, object?>
            {
                ["id"] = EntryOf(p),
                ["amount"] = Convert.ToInt32(GetMember(p, "Amount") ?? 0),
                ["extra"] = null,
            }).Cast<object?>().ToList();
        }

        string? EntryOf(object? obj) => GetMember(GetMember(obj, "Id"), "Entry")?.ToString() ?? GetMember(obj, "Entry")?.ToString();

        Dictionary<string, object?>? BuildIntentState(Creature enemy)
        {
            try
            {
                var intents = enemy.Monster?.NextMove?.Intents?.ToList();
                if (intents == null) return null;

                var intentTypes = intents.Select(i => i.IntentType.ToString()).ToList();
                int? displayDamage = null;
                int? hits = null;
                var firstAttack = intents.OfType<MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent>().FirstOrDefault();
                if (firstAttack != null && combatState.PlayerCreatures != null)
                {
                    try
                    {
                        displayDamage = firstAttack.GetTotalDamage(combatState.PlayerCreatures.ToList(), enemy);
                        if (firstAttack.Repeats > 1)
                            hits = firstAttack.Repeats;
                    }
                    catch { }
                }

                return new Dictionary<string, object?>
                {
                    ["intent_types"] = intentTypes,
                    ["total_damage"] = displayDamage,
                    ["display_damage"] = displayDamage,
                    ["hits"] = hits,
                };
            }
            catch
            {
                return null;
            }
        }

        Dictionary<string, object?> BuildCardActionMetadata(CardModel card, Creature? target = null)
        {
            var metadata = NormalizeCardPile(new[] { card })
                .OfType<Dictionary<string, object?>>()
                .FirstOrDefault() ?? new Dictionary<string, object?> { ["card_id"] = card.Id.Entry };
            metadata["target_type"] = card.TargetType.ToString();
            if (target != null)
                metadata["target_monster_id"] = target.Monster?.Id.Entry;
            return metadata;
        }

        List<object?> BuildAvailableActions(PlayerCombatState playerCombatState, IReadOnlyList<Creature> enemiesInCombat)
        {
            var actions = new List<object?>
            {
                new Dictionary<string, object?>
                {
                    ["action_type"] = "end_turn",
                    ["card_index"] = null,
                    ["target_index"] = null,
                    ["metadata"] = null,
                }
            };

            var liveEnemies = enemiesInCombat.Where(e => e != null && e.IsAlive).ToList();
            foreach (var card in playerCombatState.Hand?.Cards?.Select((c, i) => (card: c, index: i)) ?? Enumerable.Empty<(CardModel card, int index)>())
            {
                if (!card.card.CanPlay(out _, out _))
                    continue;

                if (card.card.TargetType == TargetType.AnyEnemy && liveEnemies.Count > 0)
                {
                    for (var targetIndex = 0; targetIndex < liveEnemies.Count; targetIndex++)
                    {
                        actions.Add(new Dictionary<string, object?>
                        {
                            ["action_type"] = "play_card",
                            ["card_index"] = card.index,
                            ["target_index"] = targetIndex,
                            ["metadata"] = BuildCardActionMetadata(card.card, liveEnemies[targetIndex]),
                        });
                    }
                }
                else
                {
                    actions.Add(new Dictionary<string, object?>
                    {
                        ["action_type"] = "play_card",
                        ["card_index"] = card.index,
                        ["target_index"] = null,
                        ["metadata"] = BuildCardActionMetadata(card.card),
                    });
                }
            }

            var potions = player.Potions?.Cast<object?>().ToList() ?? new List<object?>();
            for (var potionIndex = 0; potionIndex < potions.Count; potionIndex++)
            {
                var potion = potions[potionIndex];
                if (potion == null) continue;

                var targetType = GetMember(potion, "TargetType")?.ToString();
                var potionId = EntryOf(potion);

                if (string.Equals(targetType, TargetType.AnyEnemy.ToString(), StringComparison.Ordinal) && liveEnemies.Count > 0)
                {
                    for (var targetIndex = 0; targetIndex < liveEnemies.Count; targetIndex++)
                    {
                        actions.Add(new Dictionary<string, object?>
                        {
                            ["action_type"] = "use_potion",
                            ["card_index"] = null,
                            ["target_index"] = targetIndex,
                            ["metadata"] = new Dictionary<string, object?>
                            {
                                ["potion_index"] = potionIndex,
                                ["potion_id"] = potionId,
                                ["target_type"] = targetType,
                                ["target_monster_id"] = liveEnemies[targetIndex].Monster?.Id.Entry,
                            },
                        });
                    }
                }
                else
                {
                    actions.Add(new Dictionary<string, object?>
                    {
                        ["action_type"] = "use_potion",
                        ["card_index"] = null,
                        ["target_index"] = null,
                        ["metadata"] = new Dictionary<string, object?>
                        {
                            ["potion_index"] = potionIndex,
                            ["potion_id"] = potionId,
                            ["target_type"] = targetType,
                        },
                    });
                }

                actions.Add(new Dictionary<string, object?>
                {
                    ["action_type"] = "discard_potion",
                    ["card_index"] = null,
                    ["target_index"] = null,
                    ["metadata"] = new Dictionary<string, object?>
                    {
                        ["potion_index"] = potionIndex,
                        ["potion_id"] = potionId,
                    },
                });
            }

            return actions;
        }

        var liveEnemies = OrderEnemiesForSearch(combatState.Enemies?.Where(e => e != null && e.IsAlive).ToList() ?? new List<Creature>());
        var enemies = liveEnemies.Select((e, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["monster_id"] = e.Monster?.Id.Entry,
            ["hp"] = e.CurrentHp,
            ["max_hp"] = e.MaxHp,
            ["block"] = e.Block,
            ["powers"] = NormalizePowers(e.Powers),
            ["intent"] = BuildIntentState(e),
        }).Cast<object?>().ToList();

        var relics = player.Relics?
            .Select(r => new Dictionary<string, object?>
            {
                ["id"] = r.Id.Entry,
                ["extra"] = null,
            })
            .Cast<object?>()
            .ToList() ?? new List<object?>();

        state["success"] = true;
        state["character"] = (player.Character?.Id.Entry ?? "IRONCLAD").ToUpperInvariant();
        state["combat"] = new Dictionary<string, object?>
        {
            // The live-search endpoint is the authoritative input to the
            // planner. Keep the native encounter identity alongside the combat
            // snapshot so callers never need to infer it from enemy families.
            ["encounter_id"] = (_runState?.CurrentRoom as CombatRoom)?.Encounter?.Id.Entry,
            ["turn_number"] = GetMember(combatState, "TurnNumber") ?? combatState.RoundNumber,
            ["round_number"] = combatState.RoundNumber,
            ["is_player_turn"] = IsPlayPhase(),
            ["player"] = new Dictionary<string, object?>
            {
                ["hp"] = player.Creature?.CurrentHp,
                ["max_hp"] = player.Creature?.MaxHp,
                ["block"] = player.Creature?.Block,
                ["energy"] = pcs.Energy,
                ["powers"] = NormalizePowers(player.Creature?.Powers),
                ["relics"] = relics,
            },
            ["enemies"] = enemies,
            ["hand"] = NormalizeCardPile(pcs.Hand?.Cards),
            // Search state must preserve the live draw order. Sorting here makes
            // every rollout see a different top card than the engine will draw.
            ["draw_pile"] = NormalizeCardPile(pcs.DrawPile?.Cards),
            ["discard_pile"] = NormalizeCardPile(pcs.DiscardPile?.Cards),
            ["exhaust_pile"] = NormalizeCardPile(pcs.ExhaustPile?.Cards),
            ["play_pile"] = new List<object?>(),
            ["available_actions"] = BuildAvailableActions(pcs, liveEnemies),
        };
        return state;
    }

    private static List<Creature> OrderEnemiesForSearch(List<Creature> enemies)
    {
        if (enemies.Count <= 1)
            return enemies;

        static int? SnapIndexOf(Creature creature)
        {
            var slot = creature.SlotName;
            if (string.IsNullOrWhiteSpace(slot) || !slot.StartsWith("SNAP_", StringComparison.Ordinal))
                return null;
            return int.TryParse(slot.Substring(5), out var idx) ? idx : null;
        }

        var annotated = enemies.Select(c => new { Creature = c, SnapIndex = SnapIndexOf(c) }).ToList();
        if (annotated.All(x => x.SnapIndex.HasValue))
            return annotated.OrderBy(x => x.SnapIndex!.Value).Select(x => x.Creature).ToList();
        return enemies;
    }

    private static Dictionary<string, object?> BuildTransitionDiff(
        Dictionary<string, object?> actionResult,
        Dictionary<string, object?> snapshotResult)
    {
        try
        {
            var diff = new Dictionary<string, object?>();
            if (!actionResult.TryGetValue("decision", out var dObj) || !string.Equals(dObj as string, "combat_play", StringComparison.Ordinal))
            {
                diff["success"] = false;
                diff["message"] = "Action result is not combat_play decision";
                return diff;
            }

            if (!snapshotResult.TryGetValue("success", out var sObj) || sObj is not bool ok || !ok)
            {
                diff["success"] = false;
                diff["message"] = "Engine snapshot not available";
                return diff;
            }

            var snapshot = snapshotResult.TryGetValue("snapshot", out var snapObj) ? snapObj : null;
            var decisionEnergy = ToInt(actionResult, "energy");
            var decisionBlock = ExtractDecisionPlayerBlock(actionResult);
            var decisionHandCount = ExtractDecisionHandCount(actionResult);
            var decisionEnemies = ExtractDecisionEnemyHp(actionResult);

            var snapEnergy = ExtractSnapshotPlayerEnergy(snapshot);
            var snapBlock = ExtractSnapshotPlayerBlock(snapshot);
            var snapHandCount = ExtractSnapshotHandCount(snapshot);
            var snapEnemies = ExtractSnapshotEnemyHp(snapshot);

            diff["success"] = true;
            diff["energy"] = new Dictionary<string, object?> { ["decision"] = decisionEnergy, ["snapshot"] = snapEnergy, ["match"] = decisionEnergy == snapEnergy };
            diff["player_block"] = new Dictionary<string, object?> { ["decision"] = decisionBlock, ["snapshot"] = snapBlock, ["match"] = decisionBlock == snapBlock };
            diff["hand_count"] = new Dictionary<string, object?> { ["decision"] = decisionHandCount, ["snapshot"] = snapHandCount, ["match"] = decisionHandCount == snapHandCount };
            diff["enemy_hp"] = new Dictionary<string, object?>
            {
                ["decision"] = decisionEnemies,
                ["snapshot"] = snapEnemies,
                ["match"] = decisionEnemies.Count == snapEnemies.Count && decisionEnemies.SequenceEqual(snapEnemies),
            };
            return diff;
        }
        catch (Exception ex)
        {
            return new Dictionary<string, object?>
            {
                ["success"] = false,
                ["message"] = $"Diff failed: {ex.Message}",
            };
        }
    }

    private static object? ToPlainObject(object? value, int depth = 0)
    {
        if (value == null) return null;
        if (depth > 8) return value.ToString();

        var t = value.GetType();
        if (t.IsPrimitive || value is string || value is decimal || value is Guid) return value;
        if (value is Enum) return value.ToString();
        if (value is DateTime dt) return dt.ToString("O");

        if (value is System.Collections.IDictionary dict)
        {
            var d = new Dictionary<string, object?>();
            foreach (System.Collections.DictionaryEntry entry in dict)
                d[entry.Key?.ToString() ?? "null"] = ToPlainObject(entry.Value, depth + 1);
            return d;
        }

        if (value is System.Collections.IEnumerable en && value is not string)
        {
            var list = new List<object?>();
            foreach (var x in en) list.Add(ToPlainObject(x, depth + 1));
            return list;
        }

        var result = new Dictionary<string, object?>();
        var flags = BindingFlags.Instance | BindingFlags.Public;
        foreach (var p in t.GetProperties(flags))
        {
            if (!p.CanRead || p.GetIndexParameters().Length > 0) continue;
            try { result[p.Name] = ToPlainObject(p.GetValue(value), depth + 1); } catch { }
        }
        foreach (var f in t.GetFields(flags))
        {
            if (result.ContainsKey(f.Name)) continue;
            try { result[f.Name] = ToPlainObject(f.GetValue(value), depth + 1); } catch { }
        }
        return result;
    }

    private static int? ToInt(Dictionary<string, object?> dict, string key)
    {
        if (!dict.TryGetValue(key, out var v) || v == null) return null;
        try { return Convert.ToInt32(v); } catch { return null; }
    }

    private static int? ExtractDecisionPlayerBlock(Dictionary<string, object?> actionResult)
    {
        if (!actionResult.TryGetValue("player", out var pObj) || pObj is not Dictionary<string, object?> pDict) return null;
        return ToInt(pDict, "block");
    }

    private static int? ExtractDecisionHandCount(Dictionary<string, object?> actionResult)
    {
        if (!actionResult.TryGetValue("hand", out var hObj) || hObj is not IEnumerable<object?> hand) return null;
        return hand.Count();
    }

    private static List<int> ExtractDecisionEnemyHp(Dictionary<string, object?> actionResult)
    {
        var outList = new List<int>();
        if (!actionResult.TryGetValue("enemies", out var eObj) || eObj is not IEnumerable<object?> enemies) return outList;
        foreach (var e in enemies)
        {
            if (e is Dictionary<string, object?> eDict && ToInt(eDict, "hp") is int hp)
                outList.Add(hp);
        }
        return outList;
    }

    private static int? ExtractSnapshotPlayerEnergy(object? snapshot)
    {
        var players = GetMember(snapshot, "Players") as System.Collections.IEnumerable;
        var first = players?.Cast<object?>().FirstOrDefault();
        return GetIntField(first, "energy");
    }

    private static int? ExtractSnapshotPlayerBlock(object? snapshot)
    {
        var creatures = GetMember(snapshot, "Creatures") as System.Collections.IEnumerable;
        if (creatures == null) return null;
        foreach (var c in creatures)
        {
            var playerId = GetField(c, "playerId");
            if (playerId != null)
                return GetIntField(c, "block");
        }
        return null;
    }

    private static int? ExtractSnapshotHandCount(object? snapshot)
    {
        var players = GetMember(snapshot, "Players") as System.Collections.IEnumerable;
        var first = players?.Cast<object?>().FirstOrDefault();
        var piles = GetField(first, "piles") as System.Collections.IEnumerable;
        if (piles == null) return null;
        foreach (var pile in piles)
        {
            var pileType = GetField(pile, "pileType")?.ToString();
            if (string.Equals(pileType, "Hand", StringComparison.OrdinalIgnoreCase))
            {
                var cards = GetField(pile, "cards") as System.Collections.IEnumerable;
                return cards?.Cast<object?>().Count();
            }
        }
        return null;
    }

    private static List<int> ExtractSnapshotEnemyHp(object? snapshot)
    {
        var outList = new List<int>();
        var creatures = GetMember(snapshot, "Creatures") as System.Collections.IEnumerable;
        if (creatures == null) return outList;
        foreach (var c in creatures)
        {
            if (GetField(c, "playerId") == null && GetIntField(c, "currentHp") is int hp)
                outList.Add(hp);
        }
        return outList;
    }

    private static object? GetMember(object? obj, string name)
    {
        if (obj == null) return null;
        var t = obj.GetType();
        var p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public);
        if (p != null) return p.GetValue(obj);
        var f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public);
        return f?.GetValue(obj);
    }

    private static object? GetField(object? obj, string name)
    {
        if (obj == null) return null;
        return obj.GetType().GetField(name, BindingFlags.Instance | BindingFlags.Public)?.GetValue(obj);
    }

    private static int? GetIntField(object? obj, string name)
    {
        var v = GetField(obj, name);
        if (v == null) return null;
        try { return Convert.ToInt32(v); } catch { return null; }
    }

    #region Actions

    private Dictionary<string, object?> DoMapSelect(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("col") || !args.ContainsKey("row"))
            return Error("select_map_node requires 'col' and 'row'");

        var col = Convert.ToInt32(args["col"]);
        var row = Convert.ToInt32(args["row"]);
        if (col < byte.MinValue || col > byte.MaxValue ||
            row < byte.MinValue || row > byte.MaxValue)
        {
            return Error($"Map destination ({col},{row}) is outside the supported coordinate range");
        }
        var coord = new MapCoord((byte)col, (byte)row);

        // A new act starts on its Ancient node.  Entering one of that node's
        // children directly skips the Ancient (including its between-act heal),
        // so reject such coordinates even if a client sends them manually.
        var startPoint = _runState?.Map?.StartingMapPoint;
        if (_runState?.CurrentMapCoord == null && startPoint != null &&
            (coord.col != startPoint.coord.col || coord.row != startPoint.coord.row))
        {
            return Error($"Must enter the starting Ancient node at " +
                $"({startPoint.coord.col},{startPoint.coord.row}) before ({col},{row})");
        }

        var legalDestinations = LegalMapDestinations(player);
        if (!legalDestinations.Any(point =>
                point.coord.col == coord.col && point.coord.row == coord.row))
        {
            var available = string.Join(",", legalDestinations.Select(point =>
                $"({point.coord.col},{point.coord.row})"));
            return Error($"Map destination ({col},{row}) is not legal; available=[{available}]");
        }

        // Reset tracking for new room only after validating the destination.
        ResetTreasureInteractionState();
        _rewardsProcessed = false;
        _combatRewardsSet = null;
        _combatRewardsCompletion = null;
        _pendingCombatRewards.Clear();
        _activeCombatCardReward = null;
        _pendingInteractionTask = null;
        _lastKnownHp = player.Creature?.CurrentHp ?? 0;

        Log($"Moving to map coord ({col},{row}) from {DescribeRunTransitionState()}");

        // BUG-013: Wait for any pending actions (relic sessions, etc.) to complete before entering new room
        Log($"MapSelect pre-wait: {DescribeRunTransitionState()}");
        WaitForActionExecutor();
        _syncCtx.Pump();
        DrainPendingRoomTransitions("before_map_select");
        ResolvePendingRelicPicking("before_map_select");
        WaitForActionExecutor();
        _syncCtx.Pump();
        Log($"MapSelect post-wait: {DescribeRunTransitionState()}");

        // Use the same vote -> MoveToMapCoordAction path as the visible client.
        // Calling EnterMapCoord directly bypasses the synchronized map choice
        // and its native action-queue ordering.
        var mapSynchronizer = RunManager.Instance.MapSelectionSynchronizer;
        var source = new MapLocation(_runState?.CurrentMapCoord, _runState!.CurrentActIndex);
        var sourceRoom = _runState.CurrentRoom;
        if (_runState?.CurrentMapCoord is not null && mapSynchronizer == null)
            return Error("Map selection synchronizer is unavailable at a non-initial map boundary");
        var vote = new MapVote
        {
            coord = coord,
            mapGenerationCount = mapSynchronizer.MapGenerationCount,
        };
        mapSynchronizer.PlayerVotedForMapCoord(player, source, vote);

        var arrived = false;
        for (var i = 0; i < 3000; i++)
        {
            _syncCtx.Pump();
            if (RunManager.Instance.ActionExecutor.IsRunning)
                WaitForActionExecutor();
            var current = _runState?.CurrentMapCoord;
            if (current.HasValue && current.Value.col == coord.col && current.Value.row == coord.row)
            {
                arrived = true;
                break;
            }
            Thread.Sleep(5);
        }
        if (!arrived)
        {
            Log($"MapSelect timeout entering ({col},{row}): {DescribeRunTransitionState()}");
            return Error($"select_map_node timed out entering ({col},{row}) through MapSelectionSynchronizer");
        }
        Log($"MapSelect synchronized arrival at ({col},{row}): {DescribeRunTransitionState()}");
        _syncCtx.Pump();
        Log($"MapSelect post-pump: {DescribeRunTransitionState()}");
        WaitForActionExecutor();
        Log($"MapSelect post-executor: {DescribeRunTransitionState()}");

        // MoveToMapCoordAction starts GoToMapCoord with TaskHelper.RunSafely and
        // completes its queue action before the asynchronous room transition.
        // The coordinate can therefore already match while CurrentRoom is null.
        // Wait for the native EnterMapPointInternal lifecycle to install the
        // destination room before exposing the next decision boundary.
        var roomEntered = false;
        for (var i = 0; i < 3000; i++)
        {
            _syncCtx.Pump();
            if (RunManager.Instance.ActionExecutor.IsRunning)
                WaitForActionExecutor();
            var current = _runState?.CurrentMapCoord;
            var room = _runState?.CurrentRoom;
            if (current.HasValue && current.Value.col == coord.col &&
                current.Value.row == coord.row && room != null && room is not MapRoom &&
                !ReferenceEquals(room, sourceRoom))
            {
                roomEntered = true;
                break;
            }
            Thread.Sleep(5);
        }
        if (!roomEntered)
        {
            Log($"MapSelect room-entry timeout at ({col},{row}): {DescribeRunTransitionState()}");
            return Error($"select_map_node reached ({col},{row}) but destination room did not open");
        }
        Log($"MapSelect room entered at ({col},{row}): {DescribeRunTransitionState()}");

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoReconcileRelics(Player player, Dictionary<string, object?>? args)
    {
        if (_runState?.CurrentRoom is not MapRoom)
            return Error("reconcile_relics is only valid at a map boundary");
        if (args == null || !args.TryGetValue("relic_ids", out var rawIds))
            return Error("reconcile_relics requires 'relic_ids'");

        var ids = (rawIds?.ToString() ?? "")
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .ToList();
        if (ids.Count == 0)
            return Error("reconcile_relics requires at least one relic");

        var list = GetBackingList<RelicModel>(player, "_relics");
        if (list == null)
            return Error("Player relic storage is unavailable");

        var existing = list.ToList();
        var used = new bool[existing.Count];
        var reconciled = new List<RelicModel>(ids.Count);
        foreach (var id in ids)
        {
            var match = -1;
            for (var index = 0; index < existing.Count; index++)
            {
                if (!used[index]
                    && string.Equals(existing[index].Id.Entry, id, StringComparison.OrdinalIgnoreCase))
                {
                    match = index;
                    break;
                }
            }
            if (match >= 0)
            {
                used[match] = true;
                reconciled.Add(existing[match]);
                continue;
            }

            var model = ModelDb.GetById<RelicModel>(new ModelId("RELIC", id));
            if (model == null)
                return Error($"Unknown relic: {id}");
            var mutable = model.ToMutable();
            SetMaybeField(mutable, "<Owner>k__BackingField", player);
            SetMaybeField(mutable, "_owner", player);
            reconciled.Add(mutable);
        }

        list.Clear();
        list.AddRange(reconciled);
        Log($"Reconciled map relics: [{string.Join(",", ids)}]");
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoPlayCard(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("card_index"))
            return Error("play_card requires 'card_index'");

        var cardIndex = Convert.ToInt32(args["card_index"]);
        var pcs = player.PlayerCombatState;
        if (pcs == null)
            return Error("Not in combat");

        var hand = pcs.Hand.Cards;
        if (cardIndex < 0 || cardIndex >= hand.Count)
            return Error($"Invalid card index {cardIndex}, hand has {hand.Count} cards");

        var card = hand[cardIndex];

        // Determine target based on card's TargetType first
        // Self/None/All cards: target = null (game handles internally)
        // AnyEnemy cards: use target_index or auto-pick first alive enemy
        Creature? target = null;
        var cardTargetType = card.TargetType;
        if (cardTargetType == TargetType.AnyEnemy)
        {
            // Use caller's target_index if provided
            if (args.TryGetValue("target_index", out var targetObj) && targetObj != null)
            {
                var targetIndex = Convert.ToInt32(targetObj);
                var state = CombatManager.Instance.DebugOnlyGetState();
                if (state != null)
                {
                    var enemies = state.Enemies.Where(e => e != null && e.IsAlive).ToList();
                    if (targetIndex >= 0 && targetIndex < enemies.Count)
                        target = enemies[targetIndex];
                }
            }
            // Fallback: auto-target first alive enemy
            if (target == null)
            {
                var state = CombatManager.Instance.DebugOnlyGetState();
                target = state?.Enemies?.FirstOrDefault(e => e != null && e.IsAlive);
            }
        }
        // All other target types (None, All, etc.) → leave target as null

        // Check if card can be played
        if (!card.CanPlay(out var reason, out var _))
        {
            return Error($"Cannot play card {card.GetType().Name}: {reason}");
        }

        Log($"Playing card {card.GetType().Name} (index {cardIndex}) targeting {(target != null ? target.Monster?.GetType().Name ?? "creature" : "none")}");

        var handCountBefore = hand.Count;

        var playAction = new PlayCardAction(card, target);
        RunManager.Instance.ActionQueueSet.EnqueueWithoutSynchronizing(playAction);
        WaitForActionExecutor();

        // Check if card play had no effect (hand unchanged, same card still at same index)
        var handAfter = pcs.Hand.Cards;
        if (handAfter.Count == handCountBefore && cardIndex < handAfter.Count && handAfter[cardIndex] == card)
        {
            return Error($"Card could not be played (still in hand after action): {card.GetType().Name} [{card.Id}]");
        }

        WaitForPostCardDecisionBoundary(player);
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoEndTurn(Player player)
    {
        // A worker reused across search candidates can reach end_turn with stale
        // per-turn ready-sets (and/or a torn-down phase) from a prior replayed
        // line, which makes SetReadyToEndTurn early-return and the enemy turn
        // never fire (phantom survival on a lethal end_turn). Re-arm phase +
        // clear the ready-sets first; no-op on a healthy live combat.
        ReactivateCombatPhaseIfNeeded();
        if (!IsPlayPhase())
        {
            // Might be between phases — pump and check
            _syncCtx.Pump();
            if (!IsPlayPhase())
            {
                if (!CombatManager.Instance.IsInProgress || player.Creature.IsDead)
                    return DetectDecisionPoint();
                // Brief wait for ThreadPool if sync context didn't catch it
                Thread.Sleep(100);
                _syncCtx.Pump();
                if (!IsPlayPhase())
                    return DetectDecisionPoint();
            }
        }

        // Ensure no actions are still running before ending turn
        WaitForActionExecutor();

        Log($"Ending turn (round={CombatManager.Instance.DebugOnlyGetState()?.RoundNumber ?? 0})");
        _turnStarted.Reset();
        _combatEnded.Reset();

        // Keep Task.Yield continuations inline while the enemy turn advances. A
        // healthy headless turn normally completes synchronously, but allow a
        // bounded pump window for long multi-enemy turns.
        YieldPatches.SuppressYield = true;
        try
        {
            PlayerCmd.EndTurn(player, canBackOut: false);
            _syncCtx.Pump();

            for (int i = 0; i < 400; i++)
            {
                if (!CombatManager.Instance.IsInProgress || player.Creature.IsDead || IsPlayPhase()
                    || HasPendingInteractionBoundary())
                    break;
                if (_actionExecutionProfileActive)
                    _actionEndTurnPumpIterations++;
                _syncCtx.Pump();
                var endTurnSleepStarted = System.Diagnostics.Stopwatch.GetTimestamp();
                Thread.Sleep(5);
                if (_actionExecutionProfileActive)
                    _actionEndTurnPumpSleepMs += System.Diagnostics.Stopwatch
                        .GetElapsedTime(endTurnSleepStarted).TotalMilliseconds;
            }
        }
        finally
        {
            YieldPatches.SuppressYield = false;
        }

        if (HasPendingInteractionBoundary())
            return DetectDecisionPoint();

        if (CombatManager.Instance.IsInProgress && !IsPlayPhase() && !player.Creature.IsDead)
        {
            if (_actionExecutionProfileActive)
                _actionEndTurnStalls++;
            var stuckState = CombatManager.Instance.DebugOnlyGetState();
            var stuckEnemies = stuckState?.Enemies?.Where(e => e != null && e.IsAlive)
                .Select(e => $"{e.Monster?.GetType().Name}(hp={e.CurrentHp})").ToList();
            var error = Error(
                $"EndTurn stalled while player remained alive. Round={stuckState?.RoundNumber}, " +
                $"Enemies=[{string.Join(",", stuckEnemies ?? new())}], " +
                $"ActionExecutor.IsRunning={RunManager.Instance.ActionExecutor.IsRunning}");
            error["end_turn_stalled"] = true;
            error["round_number"] = stuckState?.RoundNumber;
            error["player_hp"] = player.Creature.CurrentHp;
            error["action_executor_running"] = RunManager.Instance.ActionExecutor.IsRunning;
            error["end_turn_control_state"] = InspectEndTurnControlState(player);
            return error;
        }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSelectCardReward(Player player, Dictionary<string, object?>? args)
    {
        if (_cardSelector.HasPendingReward)
        {
            if (args == null || !args.ContainsKey("card_index"))
                return Error("select_card_reward requires 'card_index'");
            var idx = Convert.ToInt32(args["card_index"]);
            var rewardCount = _cardSelector.PendingRewardCards?.Count ?? 0;
            if (idx < 0 || idx >= rewardCount)
                return Error($"Invalid event card reward index {idx}, {rewardCount} cards available");
            Log($"Resolving card reward: index {idx}");
            _cardSelector.ResolveReward(idx);
            CompletePendingRewardClaim();
            if (!AdvancePendingInteractionTask("select_card_reward"))
                return Error("Interaction did not advance after selecting its card reward");
            _activeCombatCardReward = null;
            return DetectDecisionPoint();
        }
        return Error("No pending card reward");
    }

    private Dictionary<string, object?> DoSkipCardReward(Player player)
    {
        if (_cardSelector.HasPendingReward)
        {
            if (_activeCombatCardReward?.CanSkip == false)
                return Error("This combat card reward cannot be skipped");
            Log("Skipping card reward");
            _cardSelector.SkipReward();
            CompletePendingRewardClaim();
            if (!AdvancePendingInteractionTask("skip_card_reward"))
                return Error("Interaction did not advance after skipping its card reward");
            _activeCombatCardReward = null;
            return DetectDecisionPoint();
        }
        return Error("No pending card reward");
    }

    private Dictionary<string, object?> CombatRewardsState(Player player)
    {
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "combat_reward",
            ["from_event"] = _eventRewardsSet != null,
            ["reward_set_id"] = (_eventRewardsSet ?? _combatRewardsSet)?.Id,
            ["offered_rewards"] = (_eventRewardsSet ?? _combatRewardsSet)?.Rewards.Select((reward, index) => RewardIdentity(reward, index)).ToList(),
            ["context"] = RunContext(),
            ["rewards"] = _pendingCombatRewards.Select(item => RewardIdentity(item.Reward, item.Index)).ToList(),
            ["player"] = PlayerSummary(player),
        };
    }

    private static Dictionary<string, object?> RewardIdentity(Reward reward, int index) => new()
    {
        // RewardsSetIndex is a TYPE SORT PRIORITY, not an item identity.
        // Native RewardSelectedMessage uses the item's position in set.Rewards.
        ["index"] = index,
        ["reward_type"] = reward.GetType().Name.Replace("Reward", ""),
        ["successfully_selected"] = reward.SuccessfullySelected,
        ["model_id"] = (AnyMember(reward, "Potion") as PotionModel)?.Id.Entry
            ?? (AnyMember(reward, "Relic") as RelicModel)?.Id.Entry,
        ["amount"] = reward is GoldReward gold ? gold.Amount : null,
        ["cards"] = reward is CardReward card ? card.Cards.Select(c => new { id = c.Id.Entry, upgraded = c.IsUpgraded }).ToList() : null,
    };

    private void CompletePendingRewardClaim()
    {
        if (_pendingRewardClaimTask == null) return;
        for (var i = 0; i < 200 && !_pendingRewardClaimTask.IsCompleted; i++)
        {
            _syncCtx.Pump();
            Thread.Sleep(10);
        }
        if (!_pendingRewardClaimTask.IsCompleted)
            throw new InvalidOperationException("Native reward selection did not settle");
        var task = _pendingRewardClaimTask;
        _pendingRewardClaimTask = null;
        // A card skip legitimately returns false; it resolves this choice,
        // while the set remains open until explicitly finished or abandoned.
        task.GetAwaiter().GetResult();
        _syncCtx.Pump();
    }

    private Dictionary<string, object?> DoClaimCombatReward(Player player, Dictionary<string, object?>? args)
    {
        var set = _eventRewardsSet ?? _combatRewardsSet;
        if (set == null || (_eventRewardsSet == null && _rewardsProcessed))
            return Error("No active combat rewards");
        if (args == null || !args.TryGetValue("reward_index", out var rawIndex))
            return Error("claim_combat_reward requires 'reward_index'");
        var index = Convert.ToInt32(rawIndex);
        var position = _pendingCombatRewards.FindIndex(item => item.Index == index);
        if (position < 0)
            return Error($"Combat reward index {index} is not pending");
        var reward = _pendingCombatRewards[position].Reward;
        if (args.TryGetValue("reward_set_id", out var setId) && Convert.ToInt32(setId) != set.Id)
            return Error("Reward set identity differs");
        if (_pendingRewardClaimTask != null || _cardSelector.HasPendingReward)
            return Error("Resolve the current reward choice before claiming another item");
        if (reward is CardReward cardReward)
        {
            _activeRewardIndex = index;
            _activeCombatCardReward = cardReward;
            _pendingRewardClaimTask = Task.Run(() => RunManager.Instance.RewardsSetSynchronizer.SelectLocalReward(reward));
            for (var i = 0; i < 200 && !_cardSelector.HasPendingReward && !_pendingRewardClaimTask.IsCompleted; i++)
            {
                _syncCtx.Pump();
                Thread.Sleep(10);
            }
            if (!_cardSelector.HasPendingReward)
            {
                CompletePendingRewardClaim();
                return Error("Native card reward did not reach a choice boundary");
            }
        }
        else
        {
            if (!RunManager.Instance.RewardsSetSynchronizer.SelectLocalReward(reward)
                .GetAwaiter().GetResult())
                return Error($"Combat reward was not collected: {reward.GetType().Name}");
            _syncCtx.Pump();
        }
        _pendingCombatRewards.RemoveAt(position);
        return DetectDecisionPoint();
    }

    // Retained wire name for old runtimes/reports, but no type-only no-op:
    // the new lifecycle executes an identified native reward item.
    private Dictionary<string, object?> DoAckEventReward(Player player, Dictionary<string, object?>? args)
    {
        if (_eventRewardsSet == null || args == null || !args.ContainsKey("reward_set_id"))
            return Error("ack_event_reward requires an identified native event reward");
        return DoClaimCombatReward(player, args);
    }

    private Dictionary<string, object?> DoFinishCombatRewards(Player player)
    {
        var set = _eventRewardsSet ?? _combatRewardsSet;
        if (set == null || (_eventRewardsSet == null && _rewardsProcessed))
            return Error("No active combat rewards");
        if (_cardSelector.HasPendingReward || _pendingRewardClaimTask != null
            || (_eventRewardsSet == null && _pendingInteractionTask != null))
            return Error("Resolve the current reward choice before leaving");
        if (!RunManager.Instance.RewardsSetSynchronizer.IsRewardsSetCompleted(set))
        {
            if (set.DisallowSkipping)
                return Error("This reward set cannot be abandoned");
            RunManager.Instance.RewardsSetSynchronizer.SkipLocalRewardsSet();
        }
        _pendingCombatRewards.Clear();
        if (_eventRewardsSet != null)
        {
            var completion = _eventRewardsSelection!;
            _eventRewardsSet = null;
            _eventRewardsSelection = null;
            completion.SetResult();
            if (!AdvancePendingInteractionTask("finish_event_rewards"))
                return Error("Event did not advance after finishing its rewards");
            return DetectDecisionPoint();
        }
        _rewardsProcessed = true;
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoBuyCard(Player player, Dictionary<string, object?>? args)
    {
        if (_runState?.CurrentRoom is not MerchantRoom merchantRoom)
            return Error("Not in a shop");
        if (args == null || !args.ContainsKey("card_index"))
            return Error("buy_card requires 'card_index'");

        var idx = Convert.ToInt32(args["card_index"]);
        var inventory = merchantRoom.GetLocalInventory();
        var allEntries = inventory.CharacterCardEntries
            .Concat(inventory.ColorlessCardEntries).ToList();
        if (idx < 0 || idx >= allEntries.Count)
            return Error($"Invalid card index {idx}");

        var entry = allEntries[idx];
        if (!entry.IsStocked) return Error("Card already purchased");
        if (player.Gold < entry.Cost) return Error("Not enough gold");
        if (_pendingInteractionTask != null)
            return Error("Another non-combat interaction is still running");

        try
        {
            if (!StartPendingInteraction(
                    () => entry.OnTryPurchaseWrapper(inventory), "buy_card"))
                return Error("Card purchase did not advance");
            if (_pendingInteractionTask != null)
                return DetectDecisionPoint();
            Log($"Bought card: {entry.CreationResult?.Card?.GetType().Name ?? "?"} for {entry.Cost}g");
        }
        catch (Exception ex) { return Error($"Buy card failed: {ex.Message}"); }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoBuyRelic(Player player, Dictionary<string, object?>? args)
    {
        if (_runState?.CurrentRoom is not MerchantRoom merchantRoom)
            return Error("Not in a shop");
        if (args == null || !args.ContainsKey("relic_index"))
            return Error("buy_relic requires 'relic_index'");

        var idx = Convert.ToInt32(args["relic_index"]);
        var inventory = merchantRoom.GetLocalInventory();
        var entries = inventory.RelicEntries;
        if (idx < 0 || idx >= entries.Count) return Error($"Invalid relic index {idx}");

        var entry = entries[idx];
        if (!entry.IsStocked) return Error("Relic already purchased");
        if (player.Gold < entry.Cost) return Error("Not enough gold");
        if (_pendingInteractionTask != null)
            return Error("Another non-combat interaction is still running");

        try
        {
            if (!StartPendingInteraction(
                    () => entry.OnTryPurchaseWrapper(inventory), "buy_relic"))
                return Error("Relic purchase did not advance");
            if (_pendingInteractionTask != null)
                return DetectDecisionPoint();
            Log($"Bought relic: {entry.Model.GetType().Name} for {entry.Cost}g");
        }
        catch (Exception ex) { return Error($"Buy relic failed: {ex.Message}"); }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoBuyPotion(Player player, Dictionary<string, object?>? args)
    {
        if (_runState?.CurrentRoom is not MerchantRoom merchantRoom)
            return Error("Not in a shop");
        if (args == null || !args.ContainsKey("potion_index"))
            return Error("buy_potion requires 'potion_index'");

        var idx = Convert.ToInt32(args["potion_index"]);
        var inventory = merchantRoom.GetLocalInventory();
        var entries = inventory.PotionEntries;
        if (idx < 0 || idx >= entries.Count) return Error($"Invalid potion index {idx}");

        var entry = entries[idx];
        if (!entry.IsStocked) return Error("Potion already purchased");
        if (player.Gold < entry.Cost) return Error("Not enough gold");
        if (_pendingInteractionTask != null)
            return Error("Another non-combat interaction is still running");

        try
        {
            if (!StartPendingInteraction(
                    () => entry.OnTryPurchaseWrapper(inventory), "buy_potion"))
                return Error("Potion purchase did not advance");
            if (_pendingInteractionTask != null)
                return DetectDecisionPoint();
            Log($"Bought potion: {entry.Model.GetType().Name} for {entry.Cost}g");
        }
        catch (Exception ex)
        {
            return Error($"Buy potion failed: {ex.Message}");
        }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoRemoveCard(Player player)
    {
        if (_runState?.CurrentRoom is not MerchantRoom merchantRoom)
            return Error("Not in a shop");
        if (_pendingInteractionTask != null)
            return Error("Another non-combat interaction is still running");

        var inventory = merchantRoom.GetLocalInventory();
        var removal = inventory.CardRemovalEntry;
        if (removal == null) return Error("No card removal available");
        if (player.Gold < removal.Cost) return Error("Not enough gold");

        try
        {
            if (!StartPendingInteraction(
                    () => removal.OnTryPurchaseWrapper(inventory), "remove_card"))
                return Error("Card removal did not advance");
            if (_pendingInteractionTask != null)
                return DetectDecisionPoint();
            Log($"Removed card for {removal.Cost}g");
        }
        catch (Exception ex)
        {
            _pendingInteractionTask = null;
            return Error($"Remove card failed: {ex.Message}");
        }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSelectBundle(Player player, Dictionary<string, object?>? args)
    {
        if (_pendingBundleTcs == null || _pendingBundles == null)
            return Error("No pending bundle selection");
        if (args == null || !args.ContainsKey("bundle_index"))
            return Error("select_bundle requires 'bundle_index'");

        var idx = Convert.ToInt32(args["bundle_index"]);
        if (idx < 0 || idx >= _pendingBundles.Count)
            return Error($"Invalid bundle index {idx}, {_pendingBundles.Count} bundles available");
        Log($"Bundle selection: pack {idx}");
        var bundles = _pendingBundles;
        var tcs = _pendingBundleTcs;
        _pendingBundles = null;
        _pendingBundleTcs = null;

        // Set result directly (no ContinueWith/ThreadPool)
        var selected = bundles[idx];
        tcs.TrySetResult(selected);

        if (!AdvancePendingInteractionTask("select_bundle"))
            return Error("Interaction did not advance after selecting a bundle");
        _syncCtx.Pump();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSelectCards(Player player, Dictionary<string, object?>? args)
    {
        if (!_cardSelector.HasPending)
            return Error("No pending card selection");
        if (args == null || !args.ContainsKey("indices"))
            return Error("select_cards requires 'indices' (comma-separated card indices)");

        var indicesStr = args["indices"]?.ToString() ?? "";
        var parts = indicesStr.Split(',', StringSplitOptions.RemoveEmptyEntries);
        var indices = new List<int>();
        foreach (var part in parts)
        {
            if (!int.TryParse(part.Trim(), out var index))
                return Error($"Invalid card selection index '{part}'");
            indices.Add(index);
        }
        if (indices.Count != indices.Distinct().Count())
            return Error("Card selection contains duplicate indices");
        var optionCount = _cardSelector.PendingOptions?.Count ?? 0;
        if (indices.Any(index => index < 0 || index >= optionCount))
            return Error($"Card selection index is outside 0..{Math.Max(0, optionCount - 1)}");
        if (indices.Count < _cardSelector.PendingMinSelect
            || indices.Count > _cardSelector.PendingMaxSelect)
        {
            return Error(
                $"Card selection requires {_cardSelector.PendingMinSelect}-{_cardSelector.PendingMaxSelect} cards, got {indices.Count}");
        }

        Log($"Card selection: indices [{string.Join(",", indices)}]");
        _cardSelector.ResolvePendingByIndices(indices.ToArray());
        if (!AdvancePendingInteractionTask("select_cards"))
            return Error("Interaction did not advance after selecting cards");
        _syncCtx.Pump();
        WaitForActionExecutor();

        if (HasPendingInteractionBoundary())
            return DetectDecisionPoint();

        // Extra wait for rest-site SMITH: the background ChooseLocalOption task
        // needs time to complete the upgrade after card selection resolves.
        if (_runState?.CurrentRoom is RestSiteRoom)
        {
            Thread.Sleep(200);
            _syncCtx.Pump();
            WaitForActionExecutor();
            // Force to map after SMITH completes (same pattern as HEAL)
            Log("Card selection in rest site (SMITH), forcing to map");
            ForceToMap();
            return MapSelectState();
        }

        // A shop purchase may continue after its selection resolves.
        if (_runState?.CurrentRoom is MerchantRoom)
        {
            Thread.Sleep(200);
            _syncCtx.Pump();
            WaitForActionExecutor();
            Log("Card selection in shop completed, refreshing shop state");
        }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoSkipSelect(Player player)
    {
        if (_cardSelector.HasPending)
        {
            if (_cardSelector.PendingMinSelect > 0)
                return Error($"Card selection requires at least {_cardSelector.PendingMinSelect} card(s) and cannot be skipped");
            Log("Skipping card selection");
            _cardSelector.CancelPending();
            if (!AdvancePendingInteractionTask("skip_select"))
                return Error("Interaction did not advance after skipping card selection");
            _syncCtx.Pump();
            WaitForActionExecutor();
        }
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoUsePotion(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("potion_index"))
            return Error("use_potion requires 'potion_index'");

        var idx = Convert.ToInt32(args["potion_index"]);
        var potionsList = player.Potions?.ToList() ?? new();
        if (idx < 0 || idx >= potionsList.Count) return Error($"Invalid potion index {idx}");
        var potion = potionsList[idx];
        if (potion == null) return Error($"No potion at index {idx}");

        // Determine target based on potion's TargetType first, then fall back to target_index
        Creature? target = null;
        var potionTargetType = potion.TargetType;

        // Self-targeting potions (Flex, Fortifier, etc.) ALWAYS target the player
        // regardless of any target_index the caller provides
        if (potionTargetType == TargetType.Self
            || potionTargetType == TargetType.TargetedNoCreature
            || potionTargetType.ToString() == "AnyPlayer")
        {
            target = player.Creature;
        }
        else if (potionTargetType == TargetType.AnyEnemy)
        {
            // Use caller's target_index if provided, otherwise pick first alive enemy
            if (args.TryGetValue("target_index", out var tObj) && tObj != null)
            {
                var targetIdx = Convert.ToInt32(tObj);
                var combatState = CombatManager.Instance.DebugOnlyGetState();
                if (combatState == null)
                    return Error("Cannot resolve the specified potion target outside an active combat state");
                var enemies = combatState.Enemies.Where(e => e != null && e.IsAlive).ToList();
                if (targetIdx < 0 || targetIdx >= enemies.Count)
                    return Error($"Invalid potion target index {targetIdx}; alive enemies={enemies.Count}");
                target = enemies[targetIdx];
            }
            if (target == null && CombatManager.Instance.IsInProgress)
            {
                var combatState = CombatManager.Instance.DebugOnlyGetState();
                target = combatState?.Enemies?.FirstOrDefault(e => e != null && e.IsAlive);
            }
        }
        // All other target types (None, All, etc.) → leave target as null

        Log($"Using potion: {potion.GetType().Name} at slot {idx} target={target?.GetType().Name ?? "none"}");
        try
        {
            var action = new MegaCrit.Sts2.Core.GameActions.UsePotionAction(potion, target, CombatManager.Instance.IsInProgress);
            RunManager.Instance.ActionQueueSet.EnqueueWithoutSynchronizing(action);
            WaitForActionExecutor();
            _syncCtx.Pump();

            // Effect may require card_select before the potion slot clears — do not discard as "stuck".
            if (_cardSelector.HasPending || _cardSelector.HasPendingReward)
                return DetectDecisionPoint();

            // A failed native action must not be turned into a different action.
            var afterPotions = player.Potions?.ToList() ?? new();
            if (afterPotions.Contains(potion))
                return Error($"Native potion action did not consume {potion.Id.Entry} at slot {idx}");
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("Native potion action failed", ex);
        }

        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoDiscardPotion(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("potion_index"))
            return Error("discard_potion requires 'potion_index'");

        var idx = Convert.ToInt32(args["potion_index"]);
        var potionsList = player.Potions?.ToList() ?? new();
        if (idx < 0 || idx >= potionsList.Count) return Error($"Invalid potion index {idx}");
        var potion = potionsList[idx];
        if (potion == null) return Error($"No potion at index {idx}");

        MegaCrit.Sts2.Core.Commands.PotionCmd.Discard(potion).GetAwaiter().GetResult();
        _syncCtx.Pump();
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoChooseOption(Player player, Dictionary<string, object?>? args)
    {
        if (args == null || !args.ContainsKey("option_index"))
            return Error("choose_option requires 'option_index'");

        var optionIndex = Convert.ToInt32(args["option_index"]);
        Log($"Choosing option {optionIndex}");

        // Dispatch based on ROOM TYPE (not event state) to avoid cross-contamination
        if (_runState?.CurrentRoom is RestSiteRoom restSiteRoom)
        {
            Log($"Rest site: choosing option {optionIndex}");
            if (_pendingInteractionTask != null)
                return Error("Another non-combat interaction is still running");
            try
            {
                if (!StartPendingInteraction(
                        () => RunManager.Instance.RestSiteSynchronizer.ChooseLocalOption(optionIndex),
                        "rest_option"))
                    return Error("Rest site option did not advance");
                if (_pendingInteractionTask != null)
                    return DetectDecisionPoint();
            }
            catch (Exception ex)
            {
                _pendingInteractionTask = null;
                return Error($"Rest site option failed: {ex.Message}");
            }

            // After non-Smith rest site options (HEAL, etc.), the options may not clear.
            // Wait for the action to complete (heal/dig), then force transition to map.
            if (!_cardSelector.HasPending)
            {
                Log("Rest site: option chosen (non-Smith), waiting for action then forcing to map");
                // Give the action time to complete (heal HP, dig for relic, etc.)
                WaitForActionExecutor();
                _syncCtx.Pump();
                Thread.Sleep(200);
                _syncCtx.Pump();
                WaitForActionExecutor();
                ForceToMap();
                return MapSelectState();
            }
        }
        // For events — use EventSynchronizer
        // Run Chosen() on a background thread so card selections can pause
        else if (_runState?.CurrentRoom is EventRoom)
        {
            if (_pendingInteractionTask != null)
            {
                if (!AdvancePendingInteractionTask("before_event_option"))
                    return Error("Previous non-combat interaction is still running");
                return DetectDecisionPoint();
            }
            var eventSync = RunManager.Instance.EventSynchronizer;
            var localEvent = eventSync?.GetLocalEvent();
            if (localEvent != null && !localEvent.IsFinished)
            {
                var options = localEvent.CurrentOptions;
                var optCountBefore = options?.Count ?? 0;
                if (options != null && optionIndex >= 0 && optionIndex < options.Count)
                {
                    try
                    {
                        if (!StartPendingInteraction(
                                () => options[optionIndex].Chosen(), "after_event_option"))
                            return Error("Event option did not advance");
                        if (_pendingInteractionTask != null)
                            return DetectDecisionPoint();
                    }
                    catch (Exception ex)
                    {
                        _pendingInteractionTask = null;
                        return ErrorWithTrace($"Event option failed at index {optionIndex}/{optCountBefore}: {ex.Message}", ex);
                    }
                }

                // EventChoiceState reads the actual event lifecycle. A matching
                // option count on the next page is not a reason to leave.
            }
        }

        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoCrystalSphereDivine(Dictionary<string, object?>? args)
    {
        var game = _activeCrystalSphere;
        if (_runState?.CurrentRoom is not EventRoom || game == null || game.DivinationCount <= 0)
            return Error("No active Crystal Sphere divination");
        if (args == null || !args.TryGetValue("x", out var rawX)
            || !args.TryGetValue("y", out var rawY)
            || !args.TryGetValue("tool", out var rawTool))
            return Error("crystal_sphere_divine requires x, y and tool");
        var x = Convert.ToInt32(rawX);
        var y = Convert.ToInt32(rawY);
        if (x < 0 || x >= game.GridSize.X || y < 0 || y >= game.GridSize.Y
            || !game.cells[x, y].IsHidden)
            return Error($"Crystal Sphere cell ({x},{y}) is not a hidden cell");
        var tool = rawTool?.ToString()?.ToLowerInvariant() switch
        {
            "small" => CrystalSphereMinigame.CrystalSphereToolType.Small,
            "big" => CrystalSphereMinigame.CrystalSphereToolType.Big,
            _ => CrystalSphereMinigame.CrystalSphereToolType.None,
        };
        if (tool == CrystalSphereMinigame.CrystalSphereToolType.None)
            return Error("Crystal Sphere tool must be small or big");
        var before = game.DivinationCount;
        game.SetTool(tool);
        var click = Task.Run(() => game.CellClicked(game.cells[x, y]));
        for (var i = 0; i < 200 && !click.IsCompleted; i++)
        {
            _syncCtx.Pump();
            Thread.Sleep(10);
        }
        if (!click.IsCompleted)
            return Error("Crystal Sphere click did not settle");
        click.GetAwaiter().GetResult();
        _syncCtx.Pump();
        if (game.DivinationCount != before - 1)
            return Error("Crystal Sphere click did not consume exactly one divination");
        if (game.DivinationCount == 0)
        {
            if (!AdvancePendingInteractionTask("after_crystal_sphere"))
                return Error("Crystal Sphere completion did not reach a decision boundary");
            _activeCrystalSphere = null;
        }
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> CrystalSphereState(CrystalSphereMinigame game, Player player)
    {
        var visible = new List<Dictionary<string, object?>>();
        for (var x = 0; x < game.GridSize.X; x++)
        for (var y = 0; y < game.GridSize.Y; y++)
        {
            var cell = game.cells[x, y];
            if (!cell.IsHidden)
                visible.Add(new Dictionary<string, object?>
                {
                    ["x"] = x, ["y"] = y,
                    ["item_kind"] = cell.Item?.GetType().Name,
                });
        }
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "crystal_sphere",
            ["context"] = RunContext(),
            ["player"] = PlayerSummary(player),
            ["width"] = game.GridSize.X,
            ["height"] = game.GridSize.Y,
            ["remaining"] = game.DivinationCount,
            ["visible_cells"] = visible,
        };
    }

    private bool HasPendingInteractionBoundary()
    {
        return _cardSelector.HasPending
            || _cardSelector.HasPendingReward
            || _eventRewardsSet != null
            || _activeCrystalSphere?.DivinationCount > 0
            || (_pendingBundles != null && _pendingBundleTcs != null);
    }

    private bool StartPendingInteraction(Func<Task> interaction, string reason)
    {
        if (_pendingInteractionTask != null)
            return false;
        _pendingInteractionTask = Task.Run(interaction);
        return AdvancePendingInteractionTask(reason);
    }

    private bool AdvancePendingInteractionTask(string reason)
    {
        var task = _pendingInteractionTask;
        if (task == null)
            return true;
        for (int i = 0; i < 200 && !task.IsCompleted; i++)
        {
            _syncCtx.Pump();
            if (HasPendingInteractionBoundary())
                return true;
            Thread.Sleep(10);
        }
        if (!task.IsCompleted)
        {
            Log($"Non-combat interaction did not reach a decision boundary ({reason})");
            return false;
        }
        _pendingInteractionTask = null;
        task.GetAwaiter().GetResult();
        _syncCtx.Pump();
        DrainPendingRoomTransitions(reason);
        ResolvePendingRelicPicking(reason);
        return true;
    }

    private Dictionary<string, object?> DoOpenChest(Player player)
    {
        if (_runState?.CurrentRoom is not TreasureRoom treasureRoom)
            return Error("open_chest is only valid in a treasure room");
        if (_treasureChestOpened)
            return Error("Treasure chest is already open");

        Log("Opening treasure chest");
        treasureRoom.DoNormalRewards().GetAwaiter().GetResult();
        _syncCtx.Pump();
        treasureRoom.DoExtraRewardsIfNeeded().GetAwaiter().GetResult();
        _syncCtx.Pump();
        WaitForActionExecutor();
        _treasureChestOpened = true;
        return TreasureState(treasureRoom);
    }

    private Dictionary<string, object?> DoChooseTreasureRelic(Player player, Dictionary<string, object?>? args)
    {
        if (_runState?.CurrentRoom is not TreasureRoom treasureRoom)
            return Error("choose_treasure_relic is only valid in a treasure room");
        if (!_treasureChestOpened)
            return Error("Treasure chest must be opened before choosing a relic");
        if (_treasureRelicClaimed)
            return Error("Treasure relic has already been claimed");
        if (args == null || !args.TryGetValue("relic_index", out var rawIndex))
            return Error("choose_treasure_relic requires 'relic_index'");

        var sync = RunManager.Instance.TreasureRoomRelicSynchronizer;
        var currentRelics = (GetMember(sync, "CurrentRelics") as System.Collections.IEnumerable
            ?? GetMember(sync, "_currentRelics") as System.Collections.IEnumerable)
            ?.Cast<object?>()
            .OfType<RelicModel>()
            .ToList() ?? new List<RelicModel>();
        var index = Convert.ToInt32(rawIndex);
        if (index < 0 || index >= currentRelics.Count)
            return Error($"Invalid treasure relic index {index}; available={currentRelics.Count}");

        var picked = currentRelics[index];
        var relicCountBefore = player.Relics.Count;
        Log($"Choosing treasure relic index={index} id={picked.Id.Entry}");
        List<RelicPickingResult>? awarded = null;
        void OnRelicsAwarded(List<RelicPickingResult> results) => awarded = results;
        sync.RelicsAwarded += OnRelicsAwarded;
        try
        {
            sync.PickRelicLocally(index);
            WaitForActionExecutor();
            _syncCtx.Pump();
            DrainPendingRoomTransitions("after_treasure_relic_pick");
        }
        finally
        {
            sync.RelicsAwarded -= OnRelicsAwarded;
        }

        var remaining = (GetMember(sync, "CurrentRelics") as System.Collections.IEnumerable
            ?? GetMember(sync, "_currentRelics") as System.Collections.IEnumerable)
            ?.Cast<object?>().Count() ?? 0;
        if (remaining != 0)
            return Error($"Treasure relic vote did not settle: remaining={remaining}");

        // The native synchronizer decides who receives each relic. In the
        // visible client its RelicsAwarded listener performs RelicCmd.Obtain;
        // headless must consume that same decision instead of guessing from
        // the selected index or conditionally granting a second relic.
        var localAward = awarded?.SingleOrDefault(result => result.player == player);
        if (localAward?.relic == null)
            return Error("Treasure relic synchronizer did not award a relic to the local player");
        RelicCmd.Obtain(localAward.relic.ToMutable(), player, -1).GetAwaiter().GetResult();
        _syncCtx.Pump();
        WaitForActionExecutor();
        if (player.Relics.Count <= relicCountBefore
            || !player.Relics.Any(relic => relic.Id.Entry == picked.Id.Entry))
        {
            return Error($"Treasure relic was not awarded: relic_count={player.Relics.Count}, " +
                $"expected={picked.Id.Entry}");
        }

        _treasureRelicClaimed = true;
        return TreasureState(treasureRoom);
    }

    private Dictionary<string, object?> DoLeaveRoom(Player player)
    {
        Log("Leaving room");
        try { RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult(); }
        catch { }
        _syncCtx.Pump();
        WaitForActionExecutor();

        // If still in a non-combat room, force to map
        var room = _runState?.CurrentRoom;
        if (room is RestSiteRoom || room is MerchantRoom || room is EventRoom || room is TreasureRoom)
        {
            Log("Force leaving non-combat room to map");
            try
            {
                RunManager.Instance.EnterRoom(new MapRoom()).GetAwaiter().GetResult();
                _syncCtx.Pump();
                WaitForActionExecutor();
            }
            catch (Exception ex) { Log($"Force leave: {ex.Message}"); }
        }
        return DetectDecisionPoint();
    }

    private Dictionary<string, object?> DoProceed(Player player)
    {
        Log("Proceeding");

        // Check if we need to move to next act (boss defeated)
        var room = _runState?.CurrentRoom;
        if (room is CombatRoom combatRoom && combatRoom.RoomType == RoomType.Boss)
        {
            if (combatRoom.IsPreFinished || !CombatManager.Instance.IsInProgress)
            {
                RunManager.Instance.EnterNextAct().GetAwaiter().GetResult();
                WaitForActionExecutor();
                return DetectDecisionPoint();
            }
        }

        RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult();
        WaitForActionExecutor();
        return DetectDecisionPoint();
    }

    #endregion

    #region Decision Point Detection

    private Dictionary<string, object?> DetectDecisionPoint()
    {
        if (_runState == null)
            return Error("No run in progress");

        var player = _runState.Players[0];

        // Check game over (death)
        if (player.Creature != null && player.Creature.IsDead)
        {
            return GameOverState(false);
        }

        if (_activeCrystalSphere is { DivinationCount: > 0 } sphere)
            return CrystalSphereState(sphere, player);

        // Check if there's a pending bundle selection (Scroll Boxes: pick 1 of N packs)
        if (_pendingBundles != null && _pendingBundleTcs != null && !_pendingBundleTcs.Task.IsCompleted)
        {
            var bundles = _pendingBundles.Select((bundle, i) => new Dictionary<string, object?>
            {
                ["index"] = i,
                ["cards"] = bundle.Select(card =>
                {
                    var stats = new Dictionary<string, object?>();
                    try { foreach (var dv in card.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
                    var bkws = card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                    return new Dictionary<string, object?>
                    {
                        ["id"] = card.Id.ToString(),
                        ["card_id"] = card.Id.Entry,
                        ["name"] = _loc.Card(card.Id.Entry),
                        ["cost"] = card.EnergyCost?.GetResolved() ?? 0,
                        ["type"] = card.Type.ToString(),
                        ["rarity"] = card.Rarity.ToString(),
                        ["description"] = _loc.Bilingual("cards", card.Id.Entry + ".description"),
                        ["stats"] = stats.Count > 0 ? stats : null,
                        ["keywords"] = bkws?.Count > 0 ? bkws : null,
                    };
                }).ToList(),
            }).ToList();

            return new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "bundle_select",
                ["context"] = RunContext(),
                ["bundles"] = bundles,
                ["player"] = PlayerSummary(player),
            };
        }

        // Check if there's a pending card reward from event (GetSelectedCardReward blocking)
        if (_cardSelector.HasPendingReward)
        {
            var rewardCards = _cardSelector.PendingRewardCards!;
            var cards = rewardCards.Select((cr, i) =>
            {
                var stats = new Dictionary<string, object?>();
                try { foreach (var dv in cr.Card.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
                var rrkws = cr.Card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["id"] = cr.Card.Id.ToString(),
                    ["name"] = _loc.Card(cr.Card.Id.Entry),
                    ["cost"] = cr.Card.EnergyCost?.GetResolved() ?? 0,
                    ["type"] = cr.Card.Type.ToString(),
                    ["rarity"] = cr.Card.Rarity.ToString(),
                    ["upgraded"] = cr.Card.IsUpgraded,
                    ["description"] = _loc.Bilingual("cards", cr.Card.Id.Entry + ".description"),
                    ["stats"] = stats.Count > 0 ? stats : null,
                    ["keywords"] = rrkws?.Count > 0 ? rrkws : null,
                    ["after_upgrade"] = GetUpgradedInfo(cr.Card),
                };
            }).ToList();

            return new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "card_reward",
                ["context"] = RunContext(),
                ["cards"] = cards,
                ["can_skip"] = _activeCombatCardReward?.CanSkip ?? true,
                ["from_event"] = _eventRewardsSet != null || _activeCombatCardReward == null,
                ["reward_set_id"] = (_eventRewardsSet ?? _combatRewardsSet)?.Id,
                ["reward_index"] = _activeRewardIndex,
                ["offered_rewards"] = (_eventRewardsSet ?? _combatRewardsSet)?.Rewards.Select((reward, index) => RewardIdentity(reward, index)).ToList(),
                ["gold_earned"] = _activeCombatCardReward != null ? player.Gold - _goldBeforeCombat : null,
                ["player"] = PlayerSummary(_runState!.Players[0]),
            };
        }

        // Check if there's a pending card selection (upgrade, remove, transform, start-of-turn powers)
        if (_eventRewardsSet != null)
            return CombatRewardsState(player);
        checkCardSelect:
        if (_cardSelector.HasPending && _cardSelector.PendingOptions != null)
        {
            var opts = _cardSelector.PendingOptions.Select((card, i) =>
            {
                var stats = new Dictionary<string, object?>();
                try { foreach (var dv in card.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
                var selkws = card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["id"] = card.Id.ToString(),
                    ["name"] = _loc.Card(card.Id.Entry),
                    ["cost"] = card.EnergyCost?.GetResolved() ?? 0,
                    ["type"] = card.Type.ToString(),
                    ["rarity"] = card.Rarity.ToString(),
                    ["upgraded"] = card.IsUpgraded,
                    ["stats"] = stats.Count > 0 ? stats : null,
                    ["description"] = _loc.Bilingual("cards", card.Id.Entry + ".description"),
                    ["keywords"] = selkws?.Count > 0 ? selkws : null,
                    ["after_upgrade"] = GetUpgradedInfo(card),
                };
            }).ToList();

            return new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "card_select",
                ["context"] = RunContext(),
                ["cards"] = opts,
                ["min_select"] = _cardSelector.PendingMinSelect,
                ["max_select"] = _cardSelector.PendingMaxSelect,
                ["player"] = PlayerSummary(player),
            };
        }

        // Check if there's a pending card reward
        // Check if RunManager reports game over (victory)
        if (RunManager.Instance.IsGameOver)
        {
            return GameOverState(true);
        }

        var room = _runState.CurrentRoom;

        // Map room — need to select a node
        if (room is MapRoom || room == null)
        {
            return MapSelectState();
        }

        // Combat room
        if (room is CombatRoom combatRoom)
        {
            // With Task.Yield() patched, combat init should be synchronous
            _syncCtx.Pump();
            WaitForActionExecutor();

            // Re-check for pending card selections AFTER pump (BUG-024: start-of-turn effects
            // like Tools of Trade create card selections during Pump, AFTER the initial HasPending check)
            if (_cardSelector.HasPending && _cardSelector.PendingOptions != null)
            {
                goto checkCardSelect;  // Jump back to card_select handling
            }

            if (CombatManager.Instance.IsInProgress && IsPlayPhase())
            {
                return CombatPlayState(player);
            }
            if (!CombatManager.Instance.IsInProgress || (player.Creature != null && player.Creature.IsDead))
            {
                return DetectPostCombatState(player, combatRoom);
            }
            // Fallback: brief wait
            for (int i = 0; i < 20; i++)
            {
                _syncCtx.Pump();
                Thread.Sleep(5);
                if (IsPlayPhase()) return CombatPlayState(player);
                if (!CombatManager.Instance.IsInProgress) return DetectPostCombatState(player, combatRoom);
            }
            return CombatPlayState(player);
        }

        // Event room
        if (room is EventRoom eventRoom)
        {
            return EventChoiceState(eventRoom);
        }

        // Rest site
        if (room is RestSiteRoom restRoom)
        {
            return RestSiteState(restRoom);
        }

        // Merchant/Shop
        if (room is MerchantRoom merchantRoom)
        {
            return ShopState(merchantRoom, player);
        }

        // Treasure room
        if (room is TreasureRoom treasureRoom)
        {
            return TreasureState(treasureRoom);
        }

        // Fallback
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "unknown",
            ["context"] = RunContext(),
            ["room_type"] = room?.GetType().Name,
            ["message"] = "Unknown room type or state",
        };
    }

    private bool HasWingedBootsCharge(Player player)
    {
        var boots = player.Relics?.FirstOrDefault(relic =>
            string.Equals(relic.Id.Entry, "WINGED_BOOTS", StringComparison.OrdinalIgnoreCase));
        if (boots == null)
            return false;

        // Native relic logic owns charge semantics. Rooms is remaining uses,
        // not a total against which TimesUsed can be compared again.
        return boots.ShouldAllowFreeTravel();
    }

    private List<MapPoint> LegalMapDestinations(Player player)
    {
        var map = _runState?.Map;
        if (map == null)
            return new List<MapPoint>();

        var currentCoord = _runState!.CurrentMapCoord;
        if (!currentCoord.HasValue)
            return map.StartingMapPoint == null
                ? new List<MapPoint>()
                : new List<MapPoint> { map.StartingMapPoint };

        var currentPoint = map.GetPoint(currentCoord.Value);
        var destinations = (currentPoint?.Children ?? Enumerable.Empty<MapPoint>()).ToList();
        if (currentPoint == null)
        {
            return (map.StartingMapPoint?.Children ?? Enumerable.Empty<MapPoint>()).ToList();
        }

        if (HasWingedBootsCharge(player))
        {
            var nextRow = (int)currentCoord.Value.row + 1;
            foreach (var point in map.GetPointsInRow(nextRow))
            {
                if (point != null && !destinations.Any(existing =>
                        existing.coord.col == point.coord.col && existing.coord.row == point.coord.row))
                    destinations.Add(point);
            }
        }
        return destinations;
    }

    private Dictionary<string, object?> MapSelectState()
    {
        var map = _runState?.Map;
        if (map == null)
        {
            Log("Map is null, generating...");
            try
            {
                RunManager.Instance.GenerateMap().GetAwaiter().GetResult();
                _syncCtx.Pump();
                map = _runState?.Map;
            }
            catch (Exception ex)
            {
                Log($"GenerateMap failed: {ex.Message}");
            }
            if (map == null)
                return Error("No map available");
        }
        var player = _runState!.Players[0];
        var choices = LegalMapDestinations(player)
            .Select(point => new Dictionary<string, object?>
            {
                ["col"] = (int)point.coord.col,
                ["row"] = (int)point.coord.row,
                ["type"] = point.PointType.ToString(),
            })
            .ToList();

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "map_select",
            ["context"] = RunContext(),
            ["choices"] = choices,
            ["player"] = PlayerSummary(player),
            ["act"] = _runState.CurrentActIndex + 1,
            ["act_name"] = _loc.Act(_runState.Act?.Id.Entry ?? "OVERGROWTH"),
            ["floor"] = _runState.ActFloor,
        };
    }

    private Dictionary<string, object?> CombatPlayState(Player player)
    {
        var pcs = player.PlayerCombatState;
        var combatState = CombatManager.Instance.DebugOnlyGetState();

        // Track last known HP for accurate game_over reporting (BUG-005)
        if (player.Creature != null && player.Creature.CurrentHp > 0)
            _lastKnownHp = player.Creature.CurrentHp;

        var hand = pcs?.Hand?.Cards?.Select((c, i) =>
        {
            // Extract actual stat values from DynamicVars
            var stats = new Dictionary<string, object?>();
            try
            {
                foreach (var dv in c.DynamicVars.Values)
                {
                    stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue;
                }
            }
            catch { }

            // Use CurrentStarCost (combat-modified) for UI/can_play; BaseStarCost ignores temporary reductions.
            var starCost = c.CurrentStarCost;
            var cardInfo = new Dictionary<string, object?>
            {
                ["index"] = i,
                ["id"] = c.Id.ToString(),
                ["name"] = _loc.Card(c.Id.Entry),
                ["cost"] = c.EnergyCost?.GetResolved() ?? 0,
                ["costs_x"] = c.EnergyCost?.CostsX ?? false,
                ["display_cost"] = c.EnergyCost?.GetWithModifiers((CostModifiers)(-1)) ?? 0,
                ["type"] = c.Type.ToString(),
                ["rarity"] = c.Rarity.ToString(),
                ["can_play"] = c.CanPlay(out _, out _),
                ["target_type"] = c.TargetType.ToString(),
                ["stats"] = stats.Count > 0 ? stats : null,
                ["description"] = _loc.Bilingual("cards", c.Id.Entry + ".description"),
            };
            if (starCost > 0)
            {
                cardInfo["star_cost"] = starCost;
                // BUG-007: Override can_play for star-cost cards when player lacks stars
                if (pcs != null && pcs.Stars < starCost)
                    cardInfo["can_play"] = false;
            }
            var kws = c.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
            if (kws?.Count > 0) cardInfo["keywords"] = kws;
            if (c.Enchantment != null)
            {
                cardInfo["enchantment"] = _loc.Bilingual("enchantments", c.Enchantment.Id.Entry + ".title");
                try { if (c.Enchantment.Amount != 0) cardInfo["enchantment_amount"] = c.Enchantment.Amount; } catch { }
            }
            if (c.Affliction != null)
            {
                cardInfo["affliction"] = _loc.Bilingual("afflictions", c.Affliction.Id.Entry + ".title");
                try { if (c.Affliction.Amount != 0) cardInfo["affliction_amount"] = c.Affliction.Amount; } catch { }
            }
            return cardInfo;
        }).ToList() ?? new();

        var playerCreatures = combatState?.PlayerCreatures?.ToList();

        var enemies = combatState?.Enemies?
            .Where(e => e != null && e.IsAlive)
            .Select((e, i) =>
            {
                // Extract detailed intent info
                var intents = new List<Dictionary<string, object?>>();
                try
                {
                    if (e.Monster?.NextMove?.Intents != null)
                    {
                        foreach (var intent in e.Monster.NextMove.Intents)
                        {
                            var intentInfo = new Dictionary<string, object?>
                            {
                                ["type"] = intent.IntentType.ToString(),
                            };
                            // Get damage for attack intents
                            if (intent is MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent atk && playerCreatures != null)
                            {
                                try
                                {
                                    intentInfo["damage"] = atk.GetTotalDamage(playerCreatures, e);
                                    if (atk.Repeats > 1) intentInfo["hits"] = atk.Repeats;
                                }
                                catch { }
                            }
                            intents.Add(intentInfo);
                        }
                    }
                }
                catch { }

                // Enemy powers
                var ePowers = e.Powers?.Select(pw => new Dictionary<string, object?>
                {
                    ["name"] = _loc.Power(pw.Id.Entry),
                    ["description"] = _loc.Bilingual("powers", pw.Id.Entry + ".description"),
                    ["amount"] = pw.Amount,
                }).ToList();

                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["name"] = _loc.Monster(e.Monster?.Id.Entry ?? "UNKNOWN"),
                    ["hp"] = e.CurrentHp,
                    ["max_hp"] = e.MaxHp,
                    ["block"] = e.Block,
                    ["intents"] = intents.Count > 0 ? intents : null,
                    ["intends_attack"] = e.Monster?.IntendsToAttack ?? false,
                    ["powers"] = ePowers?.Count > 0 ? ePowers : null,
                };
            }).ToList() ?? new();

        // Player powers/buffs
        var playerPowers = player.Creature?.Powers?.Select(pw => new Dictionary<string, object?>
        {
            ["name"] = _loc.Power(pw.Id.Entry),
            ["description"] = _loc.Bilingual("powers", pw.Id.Entry + ".description"),
            ["amount"] = pw.Amount,
        }).ToList();

        var result = new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "combat_play",
            ["context"] = RunContext(),
            ["round"] = combatState?.RoundNumber ?? 0,
            ["energy"] = pcs?.Energy ?? 0,
            ["max_energy"] = pcs?.MaxEnergy ?? 0,
            ["hand"] = hand,
            ["enemies"] = enemies,
            ["player"] = PlayerSummary(player),
            ["player_powers"] = playerPowers?.Count > 0 ? playerPowers : null,
            ["draw_pile_count"] = pcs?.DrawPile?.Cards?.Count ?? 0,
            ["discard_pile_count"] = pcs?.DiscardPile?.Cards?.Count ?? 0,
        };

        // Character-specific mechanics
        try
        {
            // Defect: Orbs
            var orbQueue = pcs?.OrbQueue;
            if (orbQueue?.Orbs?.Count > 0)
            {
                result["orbs"] = orbQueue.Orbs.Select((orb, i) => new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["name"] = _loc.Bilingual("orbs", orb.Id.Entry + ".title"),
                    ["type"] = orb.GetType().Name.Replace("Orb", ""),
                    ["passive"] = (int)orb.PassiveVal,
                    ["evoke"] = (int)orb.EvokeVal,
                }).ToList();
                result["orb_slots"] = orbQueue.Capacity;
            }

            // Regent: Stars
            if (pcs != null && pcs.Stars >= 0 && player.Character?.Id.Entry == "REGENT")
            {
                result["stars"] = pcs.Stars;
            }

            // Necrobinder: Osty (minion)
            var osty = player.Osty;
            if (osty != null)
            {
                result["osty"] = new Dictionary<string, object?>
                {
                    ["name"] = _loc.Monster(osty.Monster?.Id.Entry ?? "OSTY"),
                    ["hp"] = osty.CurrentHp,
                    ["max_hp"] = osty.MaxHp,
                    ["block"] = osty.Block,
                    ["alive"] = osty.IsAlive,
                };
            }
            else if (player.Character?.Id.Entry == "NECROBINDER")
            {
                result["osty"] = new Dictionary<string, object?> { ["alive"] = false };
            }
        }
        catch (Exception ex)
        {
            Log($"Character-specific data: {ex.Message}");
        }

        return result;
    }

    private Dictionary<string, object?> DetectPostCombatState(Player player, CombatRoom combatRoom)
    {
        Log($"Post-combat: RoomType={combatRoom.RoomType}, IsPreFinished={combatRoom.IsPreFinished}");
        _syncCtx.Pump();

        // CombatManager.IsInProgress can become false before the native enemy
        // list has actually reached a terminal state (notably on reused search
        // workers). Never manufacture a reward boundary from that phase flag:
        // the visible client cannot show combat rewards while a living enemy is
        // still present. Returning an explicit error preserves the evidence and
        // prevents CompactSearchActionResult from turning a phantom reward into
        // a victory leaf.
        var activeCombat = CombatManager.Instance.DebugOnlyGetState();
        var livingEnemies = activeCombat?.Enemies?
            .Where(enemy => enemy != null && enemy.IsAlive)
            .Select(enemy => $"{enemy.Monster?.GetType().Name ?? "unknown"}(hp={enemy.CurrentHp})")
            .ToList() ?? new List<string>();
        if (livingEnemies.Count > 0)
        {
            var error = Error(
                $"Combat phase ended with living enemies; refusing reward boundary. " +
                $"Enemies=[{string.Join(",", livingEnemies)}], " +
                $"IsInProgress={CombatManager.Instance.IsInProgress}, " +
                $"IsPreFinished={combatRoom.IsPreFinished}");
            error["combat_terminal_invalid"] = true;
            error["living_enemy_count"] = livingEnemies.Count;
            error["living_enemies"] = livingEnemies;
            return error;
        }

        if (!_rewardsProcessed && _combatRewardsSet == null)
        {
            _goldBeforeCombat = player.Gold;
            // The visible combat UI calls RewardsCmd.GenerateForRoomEnd and then
            // offers that one set. Headless has no UI, so start the same engine
            // reward lifecycle here. Never build a second set while observing.
            var rewardsSet = RewardsCmd.GenerateForRoomEnd(player, combatRoom)
                .GetAwaiter().GetResult();
            _combatRewardsSet = rewardsSet;
            _combatRewardsCompletion = RunManager.Instance.RewardsSetSynchronizer
                .BeginRewardsSet(rewardsSet);
            foreach (var (reward, index) in rewardsSet.Rewards.Select((reward, index) => (reward, index)))
            {
                if (reward is not (CardReward or GoldReward or MegaCrit.Sts2.Core.Rewards.RelicReward
                    or MegaCrit.Sts2.Core.Rewards.PotionReward or SpecialCardReward))
                    throw new InvalidOperationException($"Unsupported combat reward: {reward.GetType().FullName}");
                _pendingCombatRewards.Add((index, reward));
            }
        }

        if (!_rewardsProcessed)
            return CombatRewardsState(player);

        if (_combatRewardsCompletion != null)
        {
            _syncCtx.Pump();
            if (!_combatRewardsCompletion.IsCompleted)
                return Error("Combat rewards are still pending after all choices");
            _combatRewardsCompletion.GetAwaiter().GetResult();
        }
        _combatRewardsSet = null;
        _combatRewardsCompletion = null;
        _pendingCombatRewards.Clear();
        _activeCombatCardReward = null;
        _rewardsProcessed = true;

        // Boss → next act
        if (combatRoom.RoomType == RoomType.Boss)
        {
            Log("Boss defeated, entering next act");
            RunManager.Instance.EnterNextAct().GetAwaiter().GetResult();
            _syncCtx.Pump();
            WaitForActionExecutor();
            return DetectDecisionPoint();
        }

        // Normal → go to map
        ForceToMap();
        return MapSelectState();
    }

    private void ForceToMap()
    {
        DrainPendingRoomTransitions("before_force_to_map");
        ResolvePendingRelicPicking("before_force_to_map");
        try
        {
            RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult();
            _syncCtx.Pump();
        }
        catch (Exception ex) { Log($"ProceedFromTerminalRewardsScreen unavailable in headless: {ex.Message}"); }

        if (_runState?.CurrentRoom is not MapRoom)
        {
            RunManager.Instance.EnterRoom(new MapRoom()).GetAwaiter().GetResult();
            _syncCtx.Pump();
        }
    }

    private Dictionary<string, object?> EventChoiceState(EventRoom eventRoom)
    {
        var localEvent = RunManager.Instance.EventSynchronizer?.GetLocalEvent();
        _syncCtx.Pump();

        // Equal option counts do not imply completion: multi-page events often
        // keep two options throughout. The event owns its lifecycle.
        // If event is finished, proceed to map
        if (localEvent == null || localEvent.IsFinished)
        {
            Log($"Event {localEvent?.GetType().Name ?? "null"} finished, proceeding");
            try
            {
                RunManager.Instance.ProceedFromTerminalRewardsScreen().GetAwaiter().GetResult();
                _syncCtx.Pump();
            }
            catch { }
            // Force to map if still in event room
            if (_runState?.CurrentRoom is EventRoom)
            {
                try { RunManager.Instance.EnterRoom(new MapRoom()).GetAwaiter().GetResult(); _syncCtx.Pump(); }
                catch { }
            }
            return _runState?.CurrentRoom is MapRoom ? MapSelectState() : DetectDecisionPoint();
        }

        var currentOptions = localEvent.CurrentOptions;
        if (currentOptions == null || currentOptions.Count == 0)
        {
            Log($"Event {localEvent.GetType().Name} has no options, auto-skipping");
            try { RunManager.Instance.EnterRoom(new MapRoom()).GetAwaiter().GetResult(); _syncCtx.Pump(); }
            catch { }
            return MapSelectState();
        }

        var options = currentOptions
            .Select((opt, i) =>
            {
                // Try to resolve title via loc tables
                string? title = null;
                if (opt.Title != null)
                {
                    var t = _loc.Bilingual(opt.Title.LocTable, opt.Title.LocEntryKey);
                    // Check if we actually found a translation (not just the key echoed back)
                    if (t != opt.Title.LocEntryKey)
                        title = t;
                }
                // Fallback: try to extract option ID from the key and look up as relic/card/potion
                if (title == null && opt.TextKey != null)
                {
                    // TextKey like "NEOW.pages.INITIAL.options.STONE_HUMIDIFIER" → extract "STONE_HUMIDIFIER"
                    var parts = opt.TextKey.Split('.');
                    var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                    // Try relic, then card, then just use the optionId
                    var relic = _loc.Relic(optionId);
                    if (relic != optionId + ".title")
                        title = relic;
                    else
                    {
                        var card = _loc.Card(optionId);
                        if (card != optionId + ".title")
                            title = card;
                        else
                            title = optionId.Replace("_", " ");
                    }
                }
                title ??= $"option_{i}";

                // Description: try loc table first
                string? optDesc = null;
                if (opt.Description != null && !string.IsNullOrEmpty(opt.Description.LocEntryKey))
                {
                    var d = _loc.Bilingual(opt.Description.LocTable, opt.Description.LocEntryKey);
                    if (d != opt.Description.LocEntryKey)
                        optDesc = d;
                }
                // Fallback: try relic/card description
                if (optDesc == null && opt.TextKey != null)
                {
                    var parts = opt.TextKey.Split('.');
                    var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                    var rd = _loc.Bilingual("relics", optionId + ".description");
                    if (rd != optionId + ".description")
                        optDesc = rd;
                }

                // Extract vars: try event's own DynamicVars first, then relic
                Dictionary<string, object?>? optVars = null;
                try
                {
                    // Event's DynamicVars (covers Gold, HpLoss, Heal, etc.)
                    if (localEvent.DynamicVars?.Values != null)
                    {
                        optVars = new Dictionary<string, object?>();
                        foreach (var dv in localEvent.DynamicVars.Values)
                            optVars[dv.Name] = (int)dv.BaseValue;
                    }
                }
                catch { }
                // Also try relic vars (for Neow options)
                if (opt.TextKey != null)
                {
                    try
                    {
                        var parts = opt.TextKey.Split('.');
                        var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                        var relicModel = ModelDb.GetById<RelicModel>(new ModelId("RELIC", optionId));
                        if (relicModel != null)
                        {
                            optVars ??= new Dictionary<string, object?>();
                            var mutable = relicModel.ToMutable();
                            foreach (var dv in mutable.DynamicVars.Values)
                                optVars[dv.Name] = (int)dv.BaseValue;
                        }
                    }
                    catch { }
                }

                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["title"] = title,
                    ["description"] = optDesc,
                    ["text_key"] = opt.TextKey,
                    ["is_locked"] = opt.IsLocked,
                    ["vars"] = optVars?.Count > 0 ? optVars : null,
                };
            }).ToList();

        // Resolve event name — try ancients table first (for Neow), then events
        var eventEntry = localEvent.Id?.Entry ?? localEvent.GetType().Name.ToUpperInvariant();
        var eventName = _loc.Bilingual("ancients", eventEntry + ".title");
        if (eventName == eventEntry + ".title")
            eventName = _loc.Event(eventEntry);

        // Resolve event description, suppress if key not found
        string? eventDesc = null;
        if (localEvent.Description != null)
        {
            var d = _loc.Bilingual(localEvent.Description.LocTable, localEvent.Description.LocEntryKey);
            if (d != localEvent.Description.LocEntryKey)
                eventDesc = d;
        }

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "event_choice",
            ["context"] = RunContext(),
            ["event_name"] = eventName,
            ["description"] = eventDesc,
            ["options"] = options,
            ["player"] = PlayerSummary(_runState!.Players[0]),
        };
    }

    private Dictionary<string, object?> RestSiteState(RestSiteRoom restRoom)
    {
        var options = restRoom.Options;
        var player = _runState!.Players[0];

        if (options == null || options.Count == 0)
        {
            // Options empty = choice already made (synchronizer cleared them), go to map
            Log("Rest site: options empty, proceeding to map");
            ForceToMap();
            return MapSelectState();
        }

        var optionList = options.Select((opt, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["option_id"] = opt.OptionId,
            ["name"] = opt.GetType().Name,
            ["is_enabled"] = opt.IsEnabled,
        }).ToList();

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "rest_site",
            ["context"] = RunContext(),
            ["options"] = optionList,
            ["player"] = PlayerSummary(player),
        };
    }

    private Dictionary<string, object?> ShopState(MerchantRoom merchantRoom, Player player)
    {
        var inv = merchantRoom.GetLocalInventory();
        if (inv == null) { ForceToMap(); return MapSelectState(); }

        var cards = inv.CharacterCardEntries.Concat(inv.ColorlessCardEntries)
            .Select((e, i) =>
            {
                var card = e.CreationResult?.Card;
                var entry = card?.Id.Entry ?? "?";
                var stats = new Dictionary<string, object?>();
                int cardCost = 0;
                try
                {
                    if (card != null)
                    {
                        cardCost = card.EnergyCost?.GetResolved() ?? 0;
                        var mutable = card.ToMutable();
                        foreach (var dv in mutable.DynamicVars.Values)
                            stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue;
                    }
                }
                catch { }
                var shopkws = card?.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["id"] = card?.Id.ToString(),
                    ["card_id"] = entry,
                    ["name"] = _loc.Card(entry),
                    ["type"] = card?.Type.ToString() ?? "?",
                    ["rarity"] = card?.Rarity.ToString() ?? "?",
                    ["upgraded"] = card?.IsUpgraded ?? false,
                    ["card_cost"] = cardCost,
                    ["description"] = _loc.Bilingual("cards", entry + ".description"),
                    ["stats"] = stats.Count > 0 ? stats : null,
                    ["keywords"] = shopkws?.Count > 0 ? shopkws : null,
                    ["after_upgrade"] = card != null ? GetUpgradedInfo(card) : null,
                    ["cost"] = e.Cost,
                    ["is_stocked"] = e.IsStocked,
                    ["on_sale"] = e.IsOnSale,
                };
            }).ToList();

        var relics = inv.RelicEntries.Select((e, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["relic_id"] = e.Model?.Id.Entry,
            ["name"] = _loc.Relic(e.Model?.Id.Entry ?? "?"),
            ["description"] = _loc.Bilingual("relics", (e.Model?.Id.Entry ?? "?") + ".description"),
            ["cost"] = e.Cost,
            ["is_stocked"] = e.IsStocked,
        }).ToList();

        var potions = inv.PotionEntries.Select((e, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["potion_id"] = e.Model?.Id.Entry,
            ["name"] = _loc.Potion(e.Model?.Id.Entry ?? "?"),
            ["description"] = _loc.Bilingual("potions", (e.Model?.Id.Entry ?? "?") + ".description"),
            ["cost"] = e.Cost,
            ["is_stocked"] = e.IsStocked,
        }).ToList();

        var removal = inv.CardRemovalEntry;

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "shop",
            ["context"] = RunContext(),
            ["cards"] = cards,
            ["relics"] = relics,
            ["potions"] = potions,
            ["card_removal_cost"] = removal?.Cost,
            ["card_removal_available"] = removal?.IsStocked ?? false,
            ["player"] = PlayerSummary(player),
        };
    }

    private Dictionary<string, object?> TreasureState(TreasureRoom treasureRoom)
    {
        // Entering the room rolls the relic, but the visible client does not
        // grant gold or generate room-end rewards until open_chest is pressed.
        // Keep those operations at the same externally visible action boundary.
        var sync = RunManager.Instance.TreasureRoomRelicSynchronizer;
        var relics = (GetMember(sync, "CurrentRelics") as System.Collections.IEnumerable
            ?? GetMember(sync, "_currentRelics") as System.Collections.IEnumerable)
            ?.Cast<object?>()
            .OfType<RelicModel>()
            .Select((relic, index) => new Dictionary<string, object?>
            {
                ["index"] = index,
                ["id"] = relic.Id.Entry,
                ["relic_id"] = relic.Id.Entry,
                ["name"] = _loc.Relic(relic.Id.Entry),
                ["description"] = _loc.Bilingual("relics", relic.Id.Entry + ".description"),
            })
            .ToList() ?? new List<Dictionary<string, object?>>();

        if (!_treasureChestOpened)
        {
            return new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "treasure",
                ["context"] = RunContext(),
                ["opened"] = false,
                ["player"] = PlayerSummary(_runState!.Players[0]),
            };
        }

        if (!_treasureRelicClaimed)
        {
            if (relics.Count == 0)
            {
                // Silver Crucible intentionally makes the first chest empty.
                // Only accept that known native rule here; an empty list
                // without the relic remains a synchronization failure.
                var hasSilverCrucible = _runState!.Players[0].Relics.Any(
                    relic => string.Equals(relic.Id.Entry, "SILVER_CRUCIBLE", StringComparison.OrdinalIgnoreCase));
                if (hasSilverCrucible)
                {
                    return new Dictionary<string, object?>
                    {
                        ["type"] = "decision",
                        ["decision"] = "treasure_complete",
                        ["context"] = RunContext(),
                        ["opened"] = true,
                        ["claimed"] = false,
                        ["empty"] = true,
                        ["player"] = PlayerSummary(_runState!.Players[0]),
                    };
                }
                return Error("Treasure chest is open but no relic choice is active");
            }
            return new Dictionary<string, object?>
            {
                ["type"] = "decision",
                ["decision"] = "treasure_relic",
                ["context"] = RunContext(),
                ["opened"] = true,
                ["relics"] = relics,
                ["player"] = PlayerSummary(_runState!.Players[0]),
            };
        }

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "treasure_complete",
            ["context"] = RunContext(),
            ["opened"] = true,
            ["claimed"] = true,
            ["player"] = PlayerSummary(_runState!.Players[0]),
        };
    }

    private Dictionary<string, object?> GameOverState(bool isVictory)
    {
        var player = _runState!.Players[0];
        var summary = PlayerSummary(player);
        // BUG-005: When player died, the engine resets HP to max. Use last known HP instead.
        if (!isVictory)
            summary["hp"] = _lastKnownHp > 0 ? 0 : (player.Creature?.CurrentHp ?? 0);
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "game_over",
            ["context"] = RunContext(),
            ["victory"] = isVictory,
            ["player"] = summary,
            ["act"] = _runState.CurrentActIndex + 1,
            ["floor"] = _runState.ActFloor,
        };
    }

    #endregion

    #region Helpers

    private void WaitForActionExecutor()
    {
        var waitStarted = System.Diagnostics.Stopwatch.GetTimestamp();
        var iterations = 0;
        try
        {
            SynchronizationContext.SetSynchronizationContext(_syncCtx);
            _syncCtx.Pump();

            // A card choice intentionally pauses the engine action until input.
            if (_cardSelector.HasPending || _cardSelector.HasPendingReward)
                return;

            var executor = RunManager.Instance.ActionExecutor;
            for (int i = 0; i < 1000 && executor.IsRunning; i++)
            {
                iterations++;
                _syncCtx.Pump();
                if (_cardSelector.HasPending || _cardSelector.HasPendingReward)
                    return;
                if (executor.IsRunning)
                {
                    var sleepStarted = System.Diagnostics.Stopwatch.GetTimestamp();
                    Thread.Sleep(1);
                    if (_actionExecutionProfileActive)
                    {
                        _actionWaitSleepCalls++;
                        _actionWaitSleepMs += System.Diagnostics.Stopwatch
                            .GetElapsedTime(sleepStarted).TotalMilliseconds;
                    }
                }
            }
            if (executor.IsRunning)
                throw new TimeoutException("Native action executor did not settle before the next decision");
        }
        finally
        {
            if (_actionExecutionProfileActive)
            {
                _actionWaitCalls++;
                _actionWaitIterations += iterations;
                _actionWaitMaxIterations = Math.Max(_actionWaitMaxIterations, iterations);
                _actionWaitTotalMs += System.Diagnostics.Stopwatch
                    .GetElapsedTime(waitStarted).TotalMilliseconds;
            }
        }
    }

    private void WaitForPostCardDecisionBoundary(Player player)
    {
        var combatState = CombatManager.Instance.DebugOnlyGetState();
        if (combatState == null || combatState.Enemies.Any(enemy => enemy != null && enemy.IsAlive))
            return;

        // A retained dead enemy can still own the next player decision.  The
        // native power contract says whether death removes that creature;
        // AdaptablePower uses this to keep its owner for a later respawn.
        if (CombatManager.Instance.IsInProgress && IsPlayPhase()
            && combatState.Enemies.Any(enemy => enemy != null
                && enemy.Powers.Any(power => !power.ShouldCreatureBeRemovedFromCombatAfterDeath(enemy))))
            return;

        // The action executor can become idle before the combat-ended
        // continuation runs.  Returning at that point exposes a stale
        // combat_play state and skips native room-end reward generation.
        // Continue pumping only for the terminal/no-living-enemy case; normal
        // card plays return immediately and search behavior is unaffected.
        for (var i = 0; i < 400; i++)
        {
            _syncCtx.Pump();
            if (RunManager.Instance.ActionExecutor.IsRunning)
                WaitForActionExecutor();
            if (!CombatManager.Instance.IsInProgress || player.Creature == null || player.Creature.IsDead)
                return;

            combatState = CombatManager.Instance.DebugOnlyGetState();
            if (combatState?.Enemies.Any(enemy => enemy != null && enemy.IsAlive) == true)
                return;
            Thread.Sleep(5);
        }

        throw new TimeoutException(
            $"Combat did not reach a post-card decision boundary after all enemies died: " +
            DescribeRunTransitionState());
    }

    private void SpinWaitForCombatStable()
    {
        int maxIterations = 200;
        for (int i = 0; i < maxIterations; i++)
        {
            _syncCtx.Pump();
            if (!CombatManager.Instance.IsInProgress) return;
            if (IsPlayPhase()) return;
            WaitForActionExecutor();
            if (IsPlayPhase() || !CombatManager.Instance.IsInProgress) return;
            Thread.Sleep(5);
        }
    }

    private string DescribeRunTransitionState()
    {
        try
        {
            var roomName = _runState?.CurrentRoom?.GetType().Name ?? "null";
            var floor = _runState?.ActFloor;
            var coord = _runState?.CurrentMapCoord;
            var coordText = coord.HasValue ? $"({coord.Value.col},{coord.Value.row})" : "null";
            var visited = _runState?.VisitedMapCoords?.Count ?? -1;
            var executorRunning = RunManager.Instance?.ActionExecutor?.IsRunning ?? false;
            var pendingCardSelect = _cardSelector.HasPending;
            var pendingReward = _cardSelector.HasPendingReward || _pendingCombatRewards.Count > 0;
            return $"room={roomName} floor={floor} coord={coordText} visited={visited} executorRunning={executorRunning} pendingCardSelect={pendingCardSelect} pendingReward={pendingReward}";
        }
        catch (Exception ex)
        {
            return $"state_unavailable:{ex.GetType().Name}:{ex.Message}";
        }
    }

    /// <summary>Compute what a card would look like after upgrading (stats + cost + description).</summary>
    private Dictionary<string, object?>? GetUpgradedInfo(CardModel card)
    {
        if (!card.IsUpgradable) return null;
        try
        {
            var clone = ModelDb.GetById<CardModel>(card.Id).ToMutable();
            // Apply existing upgrades first
            for (int i = 0; i < card.CurrentUpgradeLevel; i++)
            {
                clone.UpgradeInternal();
                clone.FinalizeUpgradeInternal();
            }
            // Apply one more upgrade
            clone.UpgradeInternal();
            clone.FinalizeUpgradeInternal();

            var stats = new Dictionary<string, object?>();
            try { foreach (var dv in clone.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }

            // Compare keywords before/after upgrade
            var oldKws = card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToHashSet() ?? new();
            var newKws = clone.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToHashSet() ?? new();
            var addedKws = newKws.Except(oldKws).ToList();
            var removedKws = oldKws.Except(newKws).ToList();

            return new Dictionary<string, object?>
            {
                ["cost"] = clone.EnergyCost?.GetResolved() ?? 0,
                ["stats"] = stats.Count > 0 ? stats : null,
                ["description"] = _loc.Bilingual("cards", card.Id.Entry + ".description"),
                ["added_keywords"] = addedKws.Count > 0 ? addedKws : null,
                ["removed_keywords"] = removedKws.Count > 0 ? removedKws : null,
            };
        }
        catch { return null; }
    }

    private Dictionary<string, object?> PlayerSummary(Player player)
    {
        return new Dictionary<string, object?>
        {
            ["name"] = _loc.Bilingual("characters", (player.Character?.Id.Entry ?? "IRONCLAD") + ".title"),
            ["hp"] = player.Creature?.CurrentHp ?? 0,
            ["max_hp"] = player.Creature?.MaxHp ?? 0,
            ["block"] = player.Creature?.Block ?? 0,
            ["gold"] = player.Gold,
            ["relics"] = player.Relics?.Select(r =>
            {
                var vars = new Dictionary<string, object?>();
                try { foreach (var dv in r.DynamicVars.Values) vars[dv.Name] = (int)dv.BaseValue; } catch { }
                return new Dictionary<string, object?>
                {
                    ["id"] = r.Id.ToString(),
                    ["name"] = _loc.Relic(r.Id.Entry),
                    ["description"] = _loc.Bilingual("relics", r.Id.Entry + ".description"),
                    ["vars"] = vars.Count > 0 ? vars : null,
                };
            }).ToList(),
            ["potions"] = player.Potions?.Select((p, i) =>
            {
                if (p == null) return null;
                var pvars = new Dictionary<string, object?>();
                try { foreach (var dv in p.DynamicVars.Values) pvars[dv.Name] = (int)dv.BaseValue; } catch { }
                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["id"] = p.Id.ToString(),
                    ["name"] = _loc.Potion(p.Id.Entry),
                    ["description"] = _loc.Bilingual("potions", p.Id.Entry + ".description"),
                    ["vars"] = pvars.Count > 0 ? pvars : null,
                    ["target_type"] = p.TargetType.ToString(),
                };
            }).Where(x => x != null).ToList(),
            ["deck_size"] = player.Deck?.Cards?.Count(c => c != null) ?? 0,
            ["deck"] = player.Deck?.Cards?.Where(c => c != null).Select(c =>
            {
                var dstats = new Dictionary<string, object?>();
                try { foreach (var dv in c.DynamicVars.Values) dstats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
                var dkws = c.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                return new Dictionary<string, object?>
                {
                    ["id"] = c.Id.ToString(),
                    ["name"] = _loc.Card(c.Id.Entry),
                    ["cost"] = c.EnergyCost?.GetResolved() ?? 0,
                    ["type"] = c.Type.ToString(),
                    ["upgraded"] = c.IsUpgraded,
                    ["description"] = _loc.Bilingual("cards", c.Id.Entry + ".description"),
                    ["stats"] = dstats.Count > 0 ? dstats : null,
                    ["keywords"] = dkws?.Count > 0 ? dkws : null,
                    ["after_upgrade"] = GetUpgradedInfo(c),
                };
            }).ToList(),
        };
    }

    private void ApplyCapturedCombatSnapshot(
        CombatSnapshot snapshot,
        CombatRoom room,
        Player player,
        bool useCapturedMoveCallbacks)
    {
        var combatState = CombatManager.Instance.DebugOnlyGetState() ?? room.CombatState;
        var playerCombatState = player.PlayerCombatState;
        if (combatState == null || playerCombatState == null)
            throw new InvalidOperationException("Combat state missing during snapshot restore");

        SetField(combatState, "<RoundNumber>k__BackingField", snapshot.RoundNumber);
        SetField(combatState, "<CurrentSide>k__BackingField", snapshot.CurrentSide);

        var netPlayers = GetMember(snapshot.NetState, "Players") as System.Collections.IEnumerable;
        var playerSnapshot = netPlayers?.Cast<object?>().FirstOrDefault()
            ?? throw new InvalidOperationException("Snapshot missing player state");
        var netCreatures = GetMember(snapshot.NetState, "Creatures") as System.Collections.IEnumerable;
        var playerCreatureSnapshot = netCreatures?
            .Cast<object?>()
            .FirstOrDefault(c => c != null && GetMember(c, "playerId") is ulong pid && pid == player.NetId)
            ?? netCreatures?.Cast<object?>().FirstOrDefault(c => c != null && GetMember(c, "playerId") != null)
            ?? throw new InvalidOperationException("Snapshot missing player creature state");

        ApplyCreatureScalars(player.Creature, playerCreatureSnapshot);
        RebuildCreaturePowers(player.Creature, playerCreatureSnapshot);
        ApplyRelicStates(player, snapshot.RelicStates);
        RestorePotionSlots(player, snapshot);
        RestoreCombatPiles(player, playerCombatState, combatState, playerSnapshot,
            snapshot.RuntimeCardCosts);
        RestoreCombatCardDb();
        RestoreCombatCardRuntime(combatState, playerCombatState);
        ApplyPrimitiveObjectState(playerCombatState, snapshot.PlayerCombatState);
        ApplyPrimitiveObjectState(player.ExtraFields, snapshot.PlayerExtraState);
        ApplyDetailedRngStates(snapshot.RunRngStates, _runState.Rng);
        ApplyRunRngCounters(GetMember(snapshot.NetState, "Rng"), _runState.Rng);
        ApplyDetailedRngStates(snapshot.PlayerRngStates, player.PlayerRng);
        if (GetMember(playerSnapshot, "rngSet") is object playerRngSnapshot)
            ApplyPlayerRngCounters(playerRngSnapshot, player.PlayerRng);

        SetField(playerCombatState, "_energy", Convert.ToInt32(GetMember(playerSnapshot, "energy") ?? playerCombatState.Energy));
        if (GetMember(playerSnapshot, "stars") is not null)
            SetField(playerCombatState, "_stars", Convert.ToInt32(GetMember(playerSnapshot, "stars") ?? 0));
        playerCombatState.RecalculateCardValues();

        ReconcileEnemyListToSnapshot(combatState, snapshot.EnemyCreatureStates);
        var liveEnemies = combatState.Enemies?.ToList() ?? new List<Creature>();
        var snapEnemies = snapshot.EnemyCreatureStates;

        if (liveEnemies.Count != snapEnemies.Count)
            throw new InvalidOperationException($"Enemy count mismatch during restore: live={liveEnemies.Count}, snapshot={snapEnemies.Count}");

        for (var enemyIndex = 0; enemyIndex < liveEnemies.Count; enemyIndex++)
        {
            var liveEnemy = liveEnemies[enemyIndex];
            var snapEnemy = snapEnemies[enemyIndex];
            var liveId = liveEnemy.Monster?.Id.Entry ?? string.Empty;
            if (!string.Equals(liveId, snapEnemy.MonsterId, StringComparison.Ordinal))
                throw new InvalidOperationException(
                    $"Enemy order mismatch during restore at {enemyIndex}: live={liveId}, snapshot={snapEnemy.MonsterId}");
            ApplyCreatureScalars(liveEnemy, snapEnemy);
            RebuildCreaturePowers(liveEnemy, snapEnemy);
            if (enemyIndex < snapshot.EnemyAiStates.Count)
                ApplyEnemyAiState(
                    liveEnemy,
                    snapshot.EnemyAiStates[enemyIndex],
                    useCapturedMoveCallbacks);
            ApplyDetailedRngState(
                snapEnemy.MonsterRng,
                AnyMember(liveEnemy.Monster, "_rng"));
        }
        ApplyHookStates(combatState, snapshot.HookStates);
        RestoreCombatHistoryForSnapshot(snapshot, player, combatState);

        // Re-apply RNG after enemy AI restoration. Some move-state restore paths
        // (notably SetMoveImmediate) can consume hidden RNG during reconstruction.
        // The snapshot contract is that restore returns the exact saved combat
        // state, so any incidental RNG advancement during object repair must be
        // canceled before we expose the restored state.
        ApplyDetailedRngStates(snapshot.RunRngStates, _runState.Rng);
        ApplyRunRngCounters(GetMember(snapshot.NetState, "Rng"), _runState.Rng);
        ApplyDetailedRngStates(snapshot.PlayerRngStates, player.PlayerRng);
        if (GetMember(playerSnapshot, "rngSet") is object restoredPlayerRngSnapshot)
            ApplyPlayerRngCounters(restoredPlayerRngSnapshot, player.PlayerRng);
    }

    private static void ReconcileEnemyListToSnapshot(CombatState combatState, List<CombatSnapshot.EnemyCreatureSnapshot> snapEnemies)
    {
        var enemyList = AnyMember(combatState, "_enemies") as System.Collections.IList;
        if (enemyList == null)
            return;

        var hasStableSlots = snapEnemies.Count > 0
            && snapEnemies.All(enemy => !string.IsNullOrWhiteSpace(enemy.SlotName))
            && snapEnemies.Select(enemy => enemy.SlotName!).Distinct(StringComparer.Ordinal).Count()
                == snapEnemies.Count;
        if (hasStableSlots)
        {
            var liveBySlot = enemyList.Cast<object?>()
                .OfType<Creature>()
                .Where(creature => !string.IsNullOrWhiteSpace(creature.SlotName))
                .Select(creature => (Slot: creature.SlotName!, Creature: creature))
                .GroupBy(item => item.Slot, StringComparer.Ordinal)
                .ToDictionary(group => group.Key, group => group.First().Creature, StringComparer.Ordinal);
            var ordered = new List<Creature>(snapEnemies.Count);
            foreach (var desired in snapEnemies)
            {
                if (liveBySlot.TryGetValue(desired.SlotName!, out var existing)
                    && string.Equals(
                        existing.Monster?.Id.Entry,
                        desired.MonsterId,
                        StringComparison.Ordinal))
                {
                    ordered.Add(existing);
                    continue;
                }

                var canonical = ModelDb.GetById<MonsterModel>(
                    new ModelId("MONSTER", desired.MonsterId));
                var monster = canonical?.ToMutable();
                if (monster == null)
                    throw new InvalidOperationException(
                        $"Cannot recreate enemy {desired.MonsterId} in slot {desired.SlotName}");
                var recreated = combatState.CreateCreature(
                    monster, CombatSide.Enemy, desired.SlotName!);
                monster.SetUpForCombat();
                combatState.AddCreature(recreated);
                ordered.Add(recreated);
            }

            enemyList.Clear();
            for (var i = 0; i < ordered.Count; i++)
            {
                var enemy = ordered[i];
                enemy.SlotName = snapEnemies[i].SlotName!;
                enemyList.Add(enemy);
            }
            try
            {
                (AnyMember(combatState, "CreaturesChanged") as Action<CombatState>)?.Invoke(combatState);
            }
            catch { }
            return;
        }

        var desiredIds = new HashSet<string>(snapEnemies.Select(e => e.MonsterId), StringComparer.Ordinal);
        for (int i = enemyList.Count - 1; i >= 0; i--)
        {
            if (enemyList[i] is not Creature creature)
                continue;
            var liveId = creature.Monster?.Id.Entry ?? "";
            if (!desiredIds.Contains(liveId))
                enemyList.RemoveAt(i);
        }

        while (enemyList.Count > snapEnemies.Count)
            enemyList.RemoveAt(enemyList.Count - 1);

        if (enemyList.Count < snapEnemies.Count)
        {
            var liveCounts = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var existing in enemyList)
            {
                if (existing is not Creature creature)
                    continue;
                var liveId = creature.Monster?.Id.Entry ?? "";
                liveCounts[liveId] = liveCounts.GetValueOrDefault(liveId) + 1;
            }

            var desiredCounts = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var desired in snapEnemies)
                desiredCounts[desired.MonsterId] = desiredCounts.GetValueOrDefault(desired.MonsterId) + 1;

            foreach (var kv in desiredCounts)
            {
                var missing = kv.Value - liveCounts.GetValueOrDefault(kv.Key);
                if (missing <= 0)
                    continue;

                for (var createIndex = 0; createIndex < missing; createIndex++)
                {
                    var canonical = ModelDb.GetById<MonsterModel>(new ModelId("MONSTER", kv.Key));
                    var monster = canonical?.ToMutable();
                    if (monster == null)
                        continue;
                    var slot = $"RESTORE_{enemyList.Count}_{kv.Key}";
                    var creature = combatState.CreateCreature(monster, CombatSide.Enemy, slot);
                    monster.SetUpForCombat();
                    combatState.AddCreature(creature);
                }
            }
        }

        if (enemyList.Count == snapEnemies.Count)
        {
            var currentIds = new List<string>();
            foreach (var item in enemyList)
            {
                if (item is Creature creature)
                    currentIds.Add(creature.Monster?.Id.Entry ?? "");
            }

            var desiredIdSequence = snapEnemies.Select(e => e.MonsterId).ToList();
            if (currentIds.Count == desiredIdSequence.Count &&
                currentIds.SequenceEqual(desiredIdSequence, StringComparer.Ordinal))
            {
                try
                {
                    (AnyMember(combatState, "CreaturesChanged") as Action<CombatState>)?.Invoke(combatState);
                }
                catch { }
                return;
            }

            var reordered = new List<object?>();
            foreach (var desired in snapEnemies)
            {
                object? match = null;
                for (int i = 0; i < enemyList.Count; i++)
                {
                    if (enemyList[i] is not Creature creature)
                        continue;
                    var liveId = creature.Monster?.Id.Entry ?? "";
                    if (string.Equals(liveId, desired.MonsterId, StringComparison.Ordinal))
                    {
                        match = creature;
                        enemyList.RemoveAt(i);
                        break;
                    }
                }
                if (match != null)
                    reordered.Add(match);
            }

            if (reordered.Count == snapEnemies.Count)
            {
                // _enemies is now in the exact snapshot order. Do NOT call
                // SetEnemyIndex here: with no pre-set Encounter.Slots (the common
                // case, e.g. SLIMES) it does Remove+Insert(Math.Min(i, Count-1))
                // which CORRUPTS the just-fixed order — inserting the last element
                // at index 0 flips [M,S] into [S,M]. And SortEnemiesBySlotName is a
                // no-op once we overwrite SlotName with SNAP_ labels not present in
                // Encounter.Slots (IndexOf == -1 for all -> stable). The Clear+Add
                // above already placed _enemies in the correct order; the SNAP_
                // slot names make OrderEnemiesForSearch deterministic on top of
                // that. Both the raw _enemies order (read by the sanity check) and
                // the searcher's ordered view then match the snapshot exactly.
                enemyList.Clear();
                foreach (var enemy in reordered)
                    enemyList.Add(enemy);
                for (int i = 0; i < reordered.Count; i++)
                {
                    if (reordered[i] is Creature creature)
                        creature.SlotName = $"SNAP_{i:D2}";
                }
            }
        }

        if (enemyList.Count == snapEnemies.Count)
        {
            try
            {
                (AnyMember(combatState, "CreaturesChanged") as Action<CombatState>)?.Invoke(combatState);
            }
            catch { }
        }
    }

    private void ApplyRunRngCounters(object? snapshotRng, object? liveRngSet)
    {
        ApplyGenericRngCounters(snapshotRng, liveRngSet, "Counters");
    }

    private void ApplyPlayerRngCounters(object? snapshotRngSet, object? livePlayerRngSet)
    {
        ApplyGenericRngCounters(snapshotRngSet, livePlayerRngSet, "Counters");
    }

    private static List<CombatSnapshot.RngStateSnapshot> CaptureDetailedRngStates(object? rngSet)
    {
        var result = new List<CombatSnapshot.RngStateSnapshot>();
        if (rngSet == null)
            return result;

        var dict = rngSet.GetType()
            .GetField("_rngs", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?.GetValue(rngSet) as System.Collections.IDictionary;
        if (dict == null)
            return result;

        foreach (System.Collections.DictionaryEntry entry in dict)
        {
            var name = entry.Key?.ToString();
            if (string.IsNullOrWhiteSpace(name) || entry.Value == null)
                continue;

            var captured = CaptureDetailedRngState(name!, entry.Value);
            if (captured != null)
                result.Add(captured);
        }

        return result;
    }

    private static CombatSnapshot.RngStateSnapshot? CaptureDetailedRngState(
        string name, object? rng)
    {
        if (rng == null)
            return null;
        var random = AnyMember(rng, "_random");
        var impl = AnyMember(random, "_impl");
        var prng = AnyMember(impl, "_prng");
        var seedArray = AnyMember(prng, "_seedArray") as int[];
        return new CombatSnapshot.RngStateSnapshot
        {
            Name = name,
            Counter = Convert.ToInt32(AnyMember(rng, "<Counter>k__BackingField") ?? 0),
            Seed = AnyMember(rng, "<Seed>k__BackingField") is uint seed ? seed : null,
            S0 = AnyMember(random, "_s0") is ulong s0 ? s0 : null,
            S1 = AnyMember(random, "_s1") is ulong s1 ? s1 : null,
            S2 = AnyMember(random, "_s2") is ulong s2 ? s2 : null,
            S3 = AnyMember(random, "_s3") is ulong s3 ? s3 : null,
            Inext = AnyMember(prng, "_inext") as int?,
            Inextp = AnyMember(prng, "_inextp") as int?,
            SeedArray = seedArray?.ToList(),
        };
    }

    private void ApplyDetailedRngStates(List<CombatSnapshot.RngStateSnapshot> snapshots, object? liveRngSet)
    {
        if (liveRngSet == null || snapshots.Count == 0)
            return;

        var dict = liveRngSet.GetType()
            .GetField("_rngs", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?.GetValue(liveRngSet) as System.Collections.IDictionary;
        if (dict == null)
            return;

        foreach (var snap in snapshots)
        {
            object? liveRng = null;
            foreach (System.Collections.DictionaryEntry liveEntry in dict)
            {
                if (string.Equals(liveEntry.Key?.ToString(), snap.Name, StringComparison.Ordinal))
                {
                    liveRng = liveEntry.Value;
                    break;
                }
            }

            ApplyDetailedRngState(snap, liveRng);
        }
    }

    private static void ApplyDetailedRngState(
        CombatSnapshot.RngStateSnapshot? snap, object? liveRng)
    {
        if (snap == null || liveRng == null)
            return;
        if (snap.Seed.HasValue)
            SetField(liveRng, "<Seed>k__BackingField", snap.Seed.Value);

        var random = AnyMember(liveRng, "_random");
        var hasMegaState = snap.S0.HasValue && snap.S1.HasValue
            && snap.S2.HasValue && snap.S3.HasValue;
        if (hasMegaState && random != null)
        {
            SetField(liveRng, "<Counter>k__BackingField", snap.Counter);
            SetField(random, "_s0", snap.S0!.Value);
            SetField(random, "_s1", snap.S1!.Value);
            SetField(random, "_s2", snap.S2!.Value);
            SetField(random, "_s3", snap.S3!.Value);
            return;
        }

        var reinitialise = random?.GetType().GetMethod(
            "Reinitialise", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        var fastForward = liveRng.GetType().GetMethod(
            "FastForwardCounter", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null, types: new[] { typeof(int) }, modifiers: null);
        if (snap.Seed.HasValue && random != null && reinitialise != null && fastForward != null)
        {
            reinitialise.Invoke(random, new object[] { (ulong)snap.Seed.Value });
            SetField(liveRng, "<Counter>k__BackingField", 0);
            fastForward.Invoke(liveRng, new object[] { snap.Counter });
            return;
        }

        SetField(liveRng, "<Counter>k__BackingField", snap.Counter);
        var impl = AnyMember(random, "_impl");
        if (impl == null)
            return;
        var prngField = impl.GetType().GetField(
            "_prng", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        var prngBoxed = prngField?.GetValue(impl);
        if (prngField == null || prngBoxed == null)
            return;
        if (snap.Inext.HasValue)
            SetField(prngBoxed, "_inext", snap.Inext.Value);
        if (snap.Inextp.HasValue)
            SetField(prngBoxed, "_inextp", snap.Inextp.Value);
        if (snap.SeedArray is { Count: > 0 })
            SetField(prngBoxed, "_seedArray", snap.SeedArray.ToArray());
        prngField.SetValue(impl, prngBoxed);
    }

    /// <summary>
    /// Surgically re-seed named RNG stream(s) on the live run/player RNG sets,
    /// leaving every other stream's state untouched. Used for variance-reduction
    /// experiments (CRN / marginalization over draw-shuffle order): freeze the
    /// streams that define the situation (MonsterAi, map, rewards) and only
    /// re-randomize the streams that define process noise (Shuffle / draw).
    ///
    /// We reinitialize the engine's MegaRandom state from a fresh seed and zero
    /// the Counter. The fallback keeps compatibility with older System.Random
    /// runtime shapes.
    /// </summary>
    private List<string> ReseedRngStreams(object? rngSet, IReadOnlyDictionary<string, int> streamSeeds)
    {
        var changed = new List<string>();
        if (rngSet == null || streamSeeds.Count == 0)
            return changed;

        var dict = rngSet.GetType()
            .GetField("_rngs", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?.GetValue(rngSet) as System.Collections.IDictionary;
        if (dict == null)
            return changed;

        foreach (System.Collections.DictionaryEntry entry in dict)
        {
            var name = entry.Key?.ToString();
            if (string.IsNullOrWhiteSpace(name) || entry.Value == null)
                continue;
            if (!streamSeeds.TryGetValue(name!, out var newSeed))
                continue;

            var liveRng = entry.Value;
            var random = AnyMember(liveRng, "_random");
            var reinitialise = random?.GetType().GetMethod(
                "Reinitialise", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (reinitialise != null)
            {
                reinitialise.Invoke(random, new object[] { unchecked((ulong)(uint)newSeed) });
                SetField(liveRng, "<Seed>k__BackingField", unchecked((uint)newSeed));
                SetField(liveRng, "<Counter>k__BackingField", 0);
                changed.Add(name!);
                continue;
            }

            // Compatibility path for older System.Random runtime shapes.
            var fresh = new System.Random(newSeed);
            var freshImpl = AnyMember(fresh, "_impl");
            var freshPrng = AnyMember(freshImpl, "_prng");
            if (freshPrng == null)
                continue;
            var freshInext = AnyMember(freshPrng, "_inext") as int?;
            var freshInextp = AnyMember(freshPrng, "_inextp") as int?;
            var freshSeedArray = AnyMember(freshPrng, "_seedArray") as int[];
            var impl = AnyMember(random, "_impl");
            if (impl == null)
                continue;
            var prngField = impl.GetType().GetField("_prng", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            var prngBoxed = prngField?.GetValue(impl);
            if (prngField == null || prngBoxed == null)
                continue;
            if (freshInext.HasValue)
                SetField(prngBoxed, "_inext", freshInext.Value);
            if (freshInextp.HasValue)
                SetField(prngBoxed, "_inextp", freshInextp.Value);
            if (freshSeedArray is { Length: > 0 })
                SetField(prngBoxed, "_seedArray", (int[])freshSeedArray.Clone());
            prngField.SetValue(impl, prngBoxed);
            SetField(liveRng, "<Seed>k__BackingField", unchecked((uint)newSeed));
            SetField(liveRng, "<Counter>k__BackingField", 0);
            changed.Add(name!);
        }

        return changed;
    }

    /// <summary>
    /// CLI entrypoint: reseed the given streams (by name) on both the run-level
    /// and player-level RNG sets. Returns which streams were actually changed on
    /// each set so callers can assert the reseed hit its targets.
    /// </summary>
    public Dictionary<string, object?> ReseedRngStream(Dictionary<string, int> streamSeeds)
    {
        try
        {
            if (_runState == null)
                return Error("No run in progress");
            if (streamSeeds.Count == 0)
                return Error("reseed_rng_stream requires a non-empty streams map");

            var runChanged = ReseedRngStreams(_runState.Rng, streamSeeds);
            var player = _runState.Players[0];
            var playerChanged = ReseedRngStreams(player.PlayerRng, streamSeeds);

            return new Dictionary<string, object?>
            {
                ["type"] = "reseed_rng_stream_result",
                ["success"] = true,
                ["run_streams_changed"] = runChanged,
                ["player_streams_changed"] = playerChanged,
                ["requested"] = streamSeeds.Keys.ToList(),
            };
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("ReseedRngStream failed", ex);
        }
    }

    private void ApplyGenericRngCounters(object? snapshotRngLike, object? liveRngSet, string countersMemberName)
    {
        if (snapshotRngLike == null || liveRngSet == null)
            return;

        var snapshotCounters = GetMember(snapshotRngLike, countersMemberName) as System.Collections.IDictionary;
        var liveDict = liveRngSet.GetType()
            .GetField("_rngs", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?.GetValue(liveRngSet) as System.Collections.IDictionary;
        if (snapshotCounters == null || liveDict == null)
            return;

        foreach (System.Collections.DictionaryEntry entry in snapshotCounters)
        {
            var key = entry.Key?.ToString();
            if (string.IsNullOrWhiteSpace(key))
                continue;

            object? liveRng = null;
            foreach (System.Collections.DictionaryEntry liveEntry in liveDict)
            {
                if (string.Equals(liveEntry.Key?.ToString(), key, StringComparison.Ordinal))
                {
                    liveRng = liveEntry.Value;
                    break;
                }
            }

            if (liveRng == null)
                continue;

            var counter = Convert.ToInt32(entry.Value ?? 0);
            SetField(liveRng, "<Counter>k__BackingField", counter);
        }
    }

    private static void ApplyCreatureScalars(Creature? liveCreature, object snapshotCreature)
    {
        if (liveCreature == null)
            throw new InvalidOperationException("Live creature missing during restore");

        if (snapshotCreature is CombatSnapshot.EnemyCreatureSnapshot typedEnemy)
        {
            SetField(liveCreature, "_currentHp", typedEnemy.CurrentHp);
            SetField(liveCreature, "_maxHp", typedEnemy.MaxHp);
            SetField(liveCreature, "_block", typedEnemy.Block);
            if (typedEnemy.MonsterMaxHpBeforeModification.HasValue)
                SetPropertyOrField(
                    liveCreature,
                    "MonsterMaxHpBeforeModification",
                    "<MonsterMaxHpBeforeModification>k__BackingField",
                    typedEnemy.MonsterMaxHpBeforeModification.Value);
            if (typedEnemy.CombatId.HasValue)
                SetPropertyOrField(
                    liveCreature,
                    "CombatId",
                    "<CombatId>k__BackingField",
                    typedEnemy.CombatId.Value);
            if (typedEnemy.SpawnedThisTurn.HasValue)
                SetField(liveCreature.Monster, "_spawnedThisTurn", typedEnemy.SpawnedThisTurn.Value);
            return;
        }

        SetField(liveCreature, "_currentHp", Convert.ToInt32(GetMember(snapshotCreature, "currentHp") ?? liveCreature.CurrentHp));
        SetField(liveCreature, "_maxHp", Convert.ToInt32(GetMember(snapshotCreature, "maxHp") ?? liveCreature.MaxHp));
        SetField(liveCreature, "_block", Convert.ToInt32(GetMember(snapshotCreature, "block") ?? liveCreature.Block));
    }

    // Restore powers as fresh engine objects. Mutating only Amount on a reused
    // PowerModel leaves private counters, cached values and hook lifecycle state
    // from the previously explored branch. Those fields are not represented in
    // the combat snapshot, so the only generic exact restore is to unregister the
    // old objects and rebuild the snapshot list from canonical models.
    private static void RebuildCreaturePowers(Creature? liveCreature, object snapshotCreature)
    {
        if (liveCreature == null)
            throw new InvalidOperationException("Live creature missing during restore");

        List<(string Id, int Amount, int? AmountOnTurnStart)> targetPowers;
        if (snapshotCreature is CombatSnapshot.EnemyCreatureSnapshot typedEnemy)
        {
            targetPowers = typedEnemy.Powers
                .Select(p => (p.Id, p.Amount, p.AmountOnTurnStart))
                .ToList();
        }
        else
        {
            var powers = GetMember(snapshotCreature, "powers") as System.Collections.IEnumerable;
            targetPowers = (powers?.Cast<object?>() ?? Enumerable.Empty<object?>())
                .Where(p => p != null)
                .Select(p => (
                    Id: GetMember(GetMember(p!, "id"), "Entry")?.ToString()
                        ?? GetMember(p!, "id")?.ToString() ?? "",
                    Amount: Convert.ToInt32(GetMember(p!, "amount") ?? 0),
                    AmountOnTurnStart: (int?)null
                ))
                .Where(p => !string.IsNullOrWhiteSpace(p.Id))
                .ToList();
        }

        foreach (var livePower in liveCreature.Powers?.ToList() ?? new List<PowerModel>())
            // RemoveInternal fires the native Removed event before detaching the
            // power. A direct creature-list removal leaves its subscribers on
            // the reused combat state and duplicates their effects after restore.
            livePower.RemoveInternal();

        foreach (var (powerId, amount, amountOnTurnStart) in targetPowers)
        {
            var canonical = ModelDb.GetById<PowerModel>(new ModelId("POWER", powerId));
            if (canonical == null)
                throw new InvalidOperationException($"Unknown power id during restore: {powerId}");

            var power = canonical.ToMutable();
            power.ApplyInternal(liveCreature, amount, silent: true);
            if (amountOnTurnStart.HasValue)
                SetField(power, "_amountOnTurnStart", amountOnTurnStart.Value);
        }
    }

    private void RestoreCombatPiles(Player player, PlayerCombatState playerCombatState,
        CombatState combatState, object playerSnapshot,
        Dictionary<int, List<CombatSnapshot.PlainRuntimeEnergyCost>>? runtimeCardCosts)
    {
        // Pool reusable card instances from the FULL combat registry (_allCards),
        // not just the current piles. A reused (in_place) worker may carry
        // ORPHANED card instances in _allCards — cards that prior search lines
        // played (moving them out of piles) and that a later restore re-created
        // fresh instead of reusing. Those orphans stay registered in _allCards
        // forever, so it grows by a full deck per restore. ASHEN_STRIKE-style
        // cards that count cards by zone over the registry then read an inflated
        // count even though the visible piles are correct. Draw the pool from
        // _allCards so orphans are reused, and prune _allCards to exactly the
        // re-placed cards at the end so the registry can never accumulate.
        var registry = AnyMember(combatState, "_allCards") as System.Collections.IEnumerable;
        var registryCards = registry?.Cast<object?>().OfType<CardModel>().ToList()
            ?? new List<CardModel>();
        var poolSource = registryCards.Count > 0
            ? (IEnumerable<CardModel>)registryCards
            : playerCombatState.AllPiles.SelectMany(p => p.Cards);
        var pooledCards = poolSource
            .GroupBy(CardSnapshotKey)
            .ToDictionary(g => g.Key, g => new Queue<CardModel>(g), StringComparer.Ordinal);

        foreach (var pile in playerCombatState.AllPiles)
        {
            foreach (var card in pile.Cards.ToList())
            {
                pile.RemoveInternal(card, silent: true);
            }
        }

        var placedCards = new List<CardModel>();
        var pileByType = playerCombatState.AllPiles.ToDictionary(p => p.Type.ToString(), p => p, StringComparer.OrdinalIgnoreCase);
        var pileByTypeValue = playerCombatState.AllPiles.ToDictionary(p => Convert.ToInt32(p.Type), p => p);
        var pileStates = GetMember(playerSnapshot, "piles") as System.Collections.IEnumerable;
        if (pileStates == null)
            throw new InvalidOperationException("Snapshot missing combat piles");

        foreach (var pileState in pileStates)
        {
            var pileTypeObj = GetMember(pileState, "pileType");
            var pileType = pileTypeObj?.ToString();
            object? targetPile = null;
            if (pileTypeObj is int pileTypeInt && pileByTypeValue.TryGetValue(pileTypeInt, out var byValue))
                targetPile = byValue;
            else if (!string.IsNullOrWhiteSpace(pileType) && pileByType.TryGetValue(pileType, out var byName))
                targetPile = byName;
            if (targetPile is not CardPile targetPileTyped)
                continue;

            var cards = GetMember(pileState, "cards") as System.Collections.IEnumerable;
            if (cards == null)
                continue;

            var cardIndex = 0;
            foreach (var cardState in cards)
            {
                CombatSnapshot.PlainRuntimeEnergyCost? capturedRuntimeCost = null;
                if (runtimeCardCosts != null)
                {
                    var pileTypeValue = Convert.ToInt32(pileTypeObj);
                    if (!runtimeCardCosts.TryGetValue(pileTypeValue, out var costs)
                        || cardIndex >= costs.Count)
                        throw new InvalidOperationException($"Runtime card cost missing: pile={pileTypeValue} index={cardIndex}");
                    capturedRuntimeCost = costs[cardIndex];
                }
                var cardPayload = GetMember(cardState, "card")
                    ?? throw new InvalidOperationException("Snapshot card payload missing card");
                var cardKey = CardSnapshotKey(cardPayload, SnapshotKeywordNames(cardState));
                CardModel card;
                if (pooledCards.TryGetValue(cardKey, out var queue) && queue.Count > 0)
                {
                    card = queue.Dequeue();
                }
                else
                {
                    card = CreateCombatCardFromSnapshotCardState(cardPayload, cardState,
                        capturedRuntimeCost);
                    RegisterCombatCard(combatState, player, card);
                }

                SyncCardWithSnapshotState(card, cardState, capturedRuntimeCost);
                targetPileTyped.AddInternal(card, targetPileTyped.Cards.Count, silent: true);
                placedCards.Add(card);
                cardIndex++;
            }
        }

        // Prune the registry to exactly the re-placed cards: any pooled instance
        // not dequeued back into a pile is an orphan from a prior search line and
        // must not linger in _allCards (else it inflates zone-counted card stats
        // like ASHEN_STRIKE's exhaust count across reuse). Rebuild in place so we
        // keep the engine's own list object.
        if (registry is System.Collections.IList registryList)
        {
            var keep = new HashSet<CardModel>(placedCards, ReferenceEqualityComparerT<CardModel>.Instance);
            for (int i = registryList.Count - 1; i >= 0; i--)
            {
                if (registryList[i] is CardModel rc && !keep.Contains(rc))
                    registryList.RemoveAt(i);
            }
            foreach (var card in placedCards)
            {
                bool present = false;
                foreach (var item in registryList)
                    if (ReferenceEquals(item, card)) { present = true; break; }
                if (!present)
                    registryList.Add(card);
            }
        }
    }

    private static string CardSnapshotKey(object card, IEnumerable<string>? snapshotKeywords = null)
    {
        var upgradeLevel = Convert.ToInt32(GetMember(card, "CurrentUpgradeLevel") ?? 0);
        var entry = GetMember(GetMember(card, "Id"), "Entry")?.ToString()
            ?? GetMember(card, "Entry")?.ToString()
            ?? "";
        var keywordNames = snapshotKeywords ?? ((GetMember(card, "Keywords") as System.Collections.IEnumerable)
            ?.Cast<object?>()
            .Where(k => k != null && !string.Equals(k.ToString(), CardKeyword.None.ToString(), StringComparison.Ordinal))
            .Select(k => k!.ToString()!)
            .Where(k => !string.IsNullOrWhiteSpace(k))
            .OrderBy(k => k, StringComparer.Ordinal)
            ?? Enumerable.Empty<string>());
        var enchantment = GetMember(card, "enchantment") ?? GetMember(card, "Enchantment");
        var enchantmentId = ExtractModelId(enchantment)?.ToString() ?? "";
        var enchantmentAmount = enchantment == null
            ? 0
            : Convert.ToInt32(GetMember(enchantment, "amount") ?? GetMember(enchantment, "Amount") ?? 0);
        return $"{entry}#{upgradeLevel}#{string.Join(",", keywordNames)}#{enchantmentId}:{enchantmentAmount}";
    }

    private static string CardSnapshotKey(CardModel card)
    {
        return CardSnapshotKey((object)card);
    }

    private static IEnumerable<string> SnapshotKeywordNames(object cardState)
    {
        return ((GetMember(cardState, "keywords") as System.Collections.IEnumerable)
            ?? Enumerable.Empty<object?>())
            .Cast<object?>()
            .Where(k => k != null
                && !string.IsNullOrWhiteSpace(k.ToString())
                && !string.Equals(k.ToString(), CardKeyword.None.ToString(), StringComparison.OrdinalIgnoreCase))
            .Select(k => k!.ToString()!)
            .OrderBy(k => k, StringComparer.Ordinal);
    }

    private void RegisterCombatCard(CombatState combatState, Player player, CardModel card)
    {
        var addCardMethods = combatState.GetType()
            .GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .Where(m => m.Name == "AddCard")
            .OrderByDescending(m => m.GetParameters().Length)
            .ToList();

        Exception? lastError = null;
        foreach (var method in addCardMethods)
        {
            var parameters = method.GetParameters();
            try
            {
                if (parameters.Length == 2 &&
                    parameters[0].ParameterType.IsAssignableFrom(card.GetType()) &&
                    parameters[1].ParameterType.IsAssignableFrom(player.GetType()))
                {
                    method.Invoke(combatState, new object?[] { card, player });
                    return;
                }

                if (parameters.Length == 1 &&
                    parameters[0].ParameterType.IsAssignableFrom(card.GetType()))
                {
                    method.Invoke(combatState, new object?[] { card });
                    return;
                }
            }
            catch (TargetInvocationException tie)
            {
                lastError = tie.InnerException ?? tie;
            }
            catch (Exception ex)
            {
                lastError = ex;
            }
        }

        if (lastError != null)
            throw new InvalidOperationException($"Failed to register restored combat card {card.Id} with CombatState", lastError);
    }

    private CardModel CreateCombatCardFromSnapshotCardState(object serializableCard, object cardState,
        CombatSnapshot.PlainRuntimeEnergyCost? runtimeCost = null)
    {
        var modelId = ExtractModelId(serializableCard)
            ?? throw new InvalidOperationException(
                $"Snapshot card missing model id; cardType={serializableCard.GetType().FullName}; " +
                $"debugId={DebugDescribeObject(serializableCard)}; cardStateType={cardState.GetType().FullName}; " +
                $"cardState={DebugDescribeObject(cardState)}");
        var canonical = ModelDb.GetById<CardModel>(modelId)
            ?? throw new InvalidOperationException($"Unknown card id during restore: {modelId}");
        var card = canonical.ToMutable();

        var upgradeLevel = Convert.ToInt32(GetMember(serializableCard, "CurrentUpgradeLevel") ?? 0);
        for (int i = 0; i < upgradeLevel; i++)
        {
            card.UpgradeInternal();
            card.FinalizeUpgradeInternal();
        }

        SyncCardWithSnapshotState(card, cardState, runtimeCost);
        return card;
    }

    private static void RestoreCardKeywords(CardModel card, object? keywordsLike)
    {
        if (keywordsLike == null)
            return;

        var names = (keywordsLike as System.Collections.IEnumerable ?? Enumerable.Empty<object?>())
            .Cast<object?>()
            .Where(k => k != null && !string.IsNullOrWhiteSpace(k.ToString()))
            .Select(k => k!.ToString()!)
            .ToList();
        var keywords = new List<CardKeyword>();
        foreach (var name in names)
        {
            if (Enum.TryParse<CardKeyword>(name, ignoreCase: true, out var keyword) && keyword != CardKeyword.None)
                keywords.Add(keyword);
        }

        // Keywords is exposed as a read-only collection by the game model, but
        // the live instance normally owns a mutable HashSet/List underneath.
        // Mutate that collection when available so the model keeps its expected
        // concrete representation and all keyword consumers see the restored set.
        if (GetMember(card, "Keywords") is ICollection<CardKeyword> mutableKeywords)
        {
            mutableKeywords.Clear();
            foreach (var keyword in keywords)
                mutableKeywords.Add(keyword);
            return;
        }

        // Fall back to replacing a compatible private backing field for model
        // versions that expose an immutable keyword collection.
        for (var type = card.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)
                .FirstOrDefault(f => f.Name.Contains("keyword", StringComparison.OrdinalIgnoreCase));
            if (field == null)
                continue;

            object? replacement = null;
            if (field.FieldType.IsAssignableFrom(typeof(HashSet<CardKeyword>)))
                replacement = new HashSet<CardKeyword>(keywords);
            else if (field.FieldType.IsAssignableFrom(typeof(List<CardKeyword>)))
                replacement = new List<CardKeyword>(keywords);
            if (replacement != null)
            {
                field.SetValue(card, replacement);
                return;
            }
        }
    }

    private static void RestoreCardEnchantment(CardModel card, object? serializableCard)
    {
        if (card.Enchantment != null)
            card.ClearEnchantmentInternal();
        if (serializableCard == null)
            return;

        var saved = GetMember(serializableCard, "enchantment")
            ?? GetMember(serializableCard, "Enchantment");
        var enchantmentId = ExtractModelId(saved);
        if (enchantmentId == null)
            return;
        var canonical = ModelDb.GetById<EnchantmentModel>(enchantmentId)
            ?? throw new InvalidOperationException($"Unknown enchantment id during restore: {enchantmentId}");
        var amount = Convert.ToDecimal(
            GetMember(saved, "amount") ?? GetMember(saved, "Amount") ?? 0);
        var enchantment = canonical.ToMutable();
        card.EnchantInternal(enchantment, amount);

        if ((GetMember(saved, "props") ?? GetMember(saved, "Props")) is SavedProperties props)
        {
            props.Fill(enchantment);
            enchantment.RecalculateValues();
        }
    }

    private void SyncCardWithSnapshotState(CardModel card, object cardState,
        CombatSnapshot.PlainRuntimeEnergyCost? capturedRuntimeCost = null)
    {
        var savedCard = GetMember(cardState, "card");
        var floorAdded = GetMember(savedCard, "floor_added_to_deck")
            ?? GetMember(savedCard, "FloorAddedToDeck");
        if (floorAdded != null)
            card.FloorAddedToDeck = Convert.ToInt32(floorAdded);
        RestoreCardEnchantment(card, savedCard);
        RestoreCardKeywords(card, GetMember(cardState, "keywords"));

        var afflictionId = ExtractModelId(GetMember(cardState, "affliction"));
        if (afflictionId != null)
        {
            var affliction = ModelDb.GetById<AfflictionModel>(afflictionId);
            var count = Convert.ToInt32(GetMember(cardState, "afflictionCount") ?? 0);
            if (count > 0)
            {
                if (affliction == null)
                    throw new InvalidOperationException($"Unknown card affliction: {afflictionId}");
                // ModelDb returns the canonical (immutable) singleton, and
                // AfflictInternal expects full runtime apply-context (it NREs in
                // set_Amount on a bare clone). During restore we just need the
                // serialized end-state, so attach a mutable clone directly via the
                // backing fields — mirrors the power-restore path. Without this,
                // restoring a card carrying an affliction (e.g. VINE_SHAMBLER's
                // Entangled CardDebuff) threw and left the engine empty/corrupt
                // (deck vanished, enemy HP reset to max).
                var mutableAffliction = affliction.ToMutable();
                SetRequiredField(mutableAffliction, "_amount", count);
                SetRequiredField(mutableAffliction, "_card", card);
                SetRequiredField(card, "<Affliction>k__BackingField", mutableAffliction);
                if (card.Affliction?.Amount != count || !ReferenceEquals(card.Affliction.Card, card))
                    throw new InvalidOperationException("Restored card affliction did not match snapshot");
            }
            else
                SetRequiredField(card, "<Affliction>k__BackingField", null);
        }
        else
            SetRequiredField(card, "<Affliction>k__BackingField", null);

        var runtimeCost = capturedRuntimeCost ?? GetMember(cardState, "runtimeEnergyCost");
        if (runtimeCost != null)
        {
            var cost = card.EnergyCost ?? throw new InvalidOperationException("Card has no runtime energy cost");
            var canonical = Convert.ToInt32(GetMember(runtimeCost, "Canonical"));
            var costsX = Convert.ToBoolean(GetMember(runtimeCost, "CostsX"));
            if (cost.Canonical != canonical || cost.CostsX != costsX)
                throw new InvalidOperationException("Card canonical/X cost differs from snapshot");
            var modifiers = new List<LocalCostModifier>();
            if (GetMember(runtimeCost, "LocalModifiers") is System.Collections.IEnumerable capturedModifiers)
            {
                foreach (var modifier in capturedModifiers)
                {
                    modifiers.Add(new LocalCostModifier(
                        Convert.ToInt32(GetMember(modifier, "Amount")),
                        (LocalCostType)Convert.ToInt32(GetMember(modifier, "Type")),
                        (LocalCostModifierExpiration)Convert.ToInt32(GetMember(modifier, "Expiration")),
                        Convert.ToBoolean(GetMember(modifier, "IsReduceOnly"))));
                }
            }
            SetRequiredField(cost, "_base", Convert.ToInt32(GetMember(runtimeCost, "Base")));
            SetRequiredField(cost, "_capturedXValue", Convert.ToInt32(GetMember(runtimeCost, "CapturedXValue")));
            SetRequiredField(cost, "_localModifiers", modifiers);
            SetRequiredField(cost, "<WasJustUpgraded>k__BackingField",
                Convert.ToBoolean(GetMember(runtimeCost, "WasJustUpgraded")));
        }
        else
        {
            // Legacy snapshots have only the network's effective integer cost.
            // Missing Value/ResolvedValue means unknown, never zero.
            var energyCost = GetMember(cardState, "energyCost");
            var savedValue = GetMember(energyCost, "ResolvedValue") ?? GetMember(energyCost, "Value");
            if (savedValue != null && card.EnergyCost?.GetResolved() != Convert.ToInt32(savedValue))
                card.EnergyCost?.SetThisCombat(Convert.ToInt32(savedValue));
        }
    }

    private static ModelId? ExtractModelId(object? idLike)
    {
        if (idLike == null)
            return null;
        if (idLike is ModelId actual)
            return actual;

        // Support both plain exported DTOs and live runtime types:
        // - PlainModelId: { Category, Entry }
        // - PlainSerializableCard: { id = PlainModelId, ... }
        // - SerializableCard / runtime card-like objects: { Id / id = ModelId-like }
        if (GetMember(idLike, "id") is object nestedLowerId)
            return ExtractModelId(nestedLowerId);
        if (GetMember(idLike, "Id") is object nestedUpperId)
            return ExtractModelId(nestedUpperId);

        var category = GetMember(idLike, "Category")?.ToString() ?? GetMember(idLike, "category")?.ToString();
        var entry = GetMember(idLike, "Entry")?.ToString() ?? GetMember(idLike, "entry")?.ToString();
        if (string.IsNullOrWhiteSpace(category) || string.IsNullOrWhiteSpace(entry))
            return null;
        return new ModelId(category!, entry!);
    }

    private static string DebugDescribeObject(object? obj)
    {
        if (obj == null)
            return "null";
        try
        {
            var t = obj.GetType();
            var members = new List<string>();
            foreach (var f in t.GetFields(BindingFlags.Instance | BindingFlags.Public))
            {
                object? value = null;
                try { value = f.GetValue(obj); } catch { }
                members.Add($"{f.Name}={value}");
            }
            foreach (var p in t.GetProperties(BindingFlags.Instance | BindingFlags.Public))
            {
                if (!p.CanRead || p.GetIndexParameters().Length > 0)
                    continue;
                object? value = null;
                try { value = p.GetValue(obj); } catch { }
                members.Add($"{p.Name}={value}");
            }
            return string.Join(", ", members.Take(12));
        }
        catch (Exception ex)
        {
            return $"<debug_failed:{ex.GetType().Name}>";
        }
    }

    // Find the compiler-generated backing delegate field for an event. C# event
    // backing fields usually share the event's name, but explicit/custom events
    // may differ — fall back to scanning for a delegate field whose name contains
    // the event name.
    private static FieldInfo? FindEventBackingField(Type type, string eventName)
    {
        const BindingFlags bf = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        for (var t = type; t != null; t = t.BaseType)
        {
            var direct = t.GetField(eventName, bf);
            if (direct != null && typeof(Delegate).IsAssignableFrom(direct.FieldType))
                return direct;
            var match = t.GetFields(bf).FirstOrDefault(f =>
                typeof(Delegate).IsAssignableFrom(f.FieldType) &&
                f.Name.IndexOf(eventName, StringComparison.Ordinal) >= 0);
            if (match != null) return match;
        }
        return null;
    }

    // Debug: remove ALL handlers tracked in NetCombatCardDb._subscriptions from
    // their piles' ContentsChanged events (each is a distinct stale StartCombat
    // closure from a prior restore, NOT a dedup-able duplicate). Tests whether the
    // accumulated stale subscriptions are what inflate per-card-play damage.
    public Dictionary<string, object?> DedupPileHandlers()
    {
        try
        {
            var dbType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb");
            var instance = dbType?.GetProperty("Instance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)?.GetValue(null);
            var subsField = dbType?.GetField("_subscriptions", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            int removed = 0;
            if (instance != null && subsField?.GetValue(instance) is System.Collections.IList subs)
            {
                foreach (var sub in subs)
                {
                    if (sub == null) continue;
                    var st = sub.GetType();
                    var pileObj = st.GetField("pile", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(sub);
                    var handler = st.GetField("action", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(sub) as Delegate;
                    if (pileObj == null || handler == null) continue;
                    var evtField = FindEventBackingField(pileObj.GetType(), "ContentsChanged");
                    if (evtField?.GetValue(pileObj) is Delegate cur)
                    {
                        var before = cur.GetInvocationList().Length;
                        var after = Delegate.Remove(cur, handler);
                        try { evtField.SetValue(pileObj, after); } catch { }
                        removed += before - (after?.GetInvocationList().Length ?? 0);
                    }
                }
            }
            return new Dictionary<string, object?> { ["success"] = true, ["removed"] = removed };
        }
        catch (Exception ex) { return ErrorWithTrace("DedupPileHandlers failed", ex); }
    }

    // Reset the live combat event history on snapshot restore. CombatManager's
    // History (CombatHistory) logs per-turn events (CardExhausted, CardPlayed,
    // EnergySpent, …) that effects query — e.g. FORGOTTEN_RITUAL ("if you
    // Exhausted a card this turn, gain energy") reads CardExhausted entries. On a
    // reused (in_place) worker the history is NOT rebuilt, so events from prior
    // search lines accumulate (+5/restore here) and "this turn" queries see stale
    // exhaust/play events — FORGOTTEN_RITUAL wrongly grants energy, etc. A cold
    // full restore starts the combat fresh so its history reflects only the
    // restored state. Clearing here removes the leaked entries so an in_place
    // restore matches the cold "no events accumulated since the snapshot" state.
    private void ResetCombatHistoryForRestore()
    {
        try
        {
            var history = CombatManager.Instance?.History;
            if (history == null) return;
            var clear = history.GetType().GetMethod("Clear", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            clear?.Invoke(history, Array.Empty<object?>());
        }
        catch { }
    }

    private static PrimitiveObjectStateSnapshot? CapturePrimitiveObjectState(object? instance, int typeOrdinal = 0)
    {
        if (instance == null) return null;
        var fields = new List<PrimitiveFieldSnapshot>();
        for (var type = instance.GetType(); type != null; type = type.BaseType)
        {
            foreach (var field in type.GetFields(
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic |
                BindingFlags.DeclaredOnly))
            {
                if (field.IsInitOnly) continue;
                var fieldType = field.FieldType;
                if (!(fieldType.IsPrimitive || fieldType.IsEnum || fieldType == typeof(string) ||
                      fieldType == typeof(decimal)))
                    continue;
                object? value;
                try { value = field.GetValue(instance); } catch { continue; }
                if (value is Enum) value = value.ToString();
                fields.Add(new PrimitiveFieldSnapshot
                {
                    DeclaringType = type.FullName ?? type.Name,
                    Name = field.Name,
                    FieldType = fieldType.AssemblyQualifiedName ?? fieldType.FullName ?? fieldType.Name,
                    Value = value,
                });
            }
        }
        return new PrimitiveObjectStateSnapshot
        {
            TypeName = instance.GetType().FullName ?? instance.GetType().Name,
            TypeOrdinal = typeOrdinal,
            Fields = fields,
        };
    }

    private static void ApplyPrimitiveObjectState(object? instance, PrimitiveObjectStateSnapshot? snapshot)
    {
        if (instance == null || snapshot == null) return;
        foreach (var fieldSnapshot in snapshot.Fields)
        {
            var declaringType = instance.GetType();
            while (declaringType != null &&
                   !string.Equals(declaringType.FullName, fieldSnapshot.DeclaringType, StringComparison.Ordinal))
                declaringType = declaringType.BaseType;
            var field = declaringType?.GetField(
                fieldSnapshot.Name,
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic |
                BindingFlags.DeclaredOnly);
            if (field == null || field.IsInitOnly) continue;
            try
            {
                field.SetValue(instance, CoerceRelicFieldValue(fieldSnapshot.Value, field.FieldType));
            }
            catch { }
        }
    }

    private static List<PrimitiveObjectStateSnapshot> CaptureHookStates(CombatState combatState)
    {
        var result = new List<PrimitiveObjectStateSnapshot>();
        var method = combatState.GetType().GetMethod(
            "IterateHookListeners", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (method?.Invoke(combatState, Array.Empty<object?>()) is not System.Collections.IEnumerable listeners)
            return result;
        var ordinals = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var listener in listeners)
        {
            if (listener == null) continue;
            var typeName = listener.GetType().FullName ?? listener.GetType().Name;
            var ordinal = ordinals.GetValueOrDefault(typeName);
            ordinals[typeName] = ordinal + 1;
            var state = CapturePrimitiveObjectState(listener, ordinal);
            if (state != null && state.Fields.Count > 0)
                result.Add(state);
        }
        return result;
    }

    private static void ApplyHookStates(CombatState combatState, List<PrimitiveObjectStateSnapshot>? snapshots)
    {
        if (snapshots == null || snapshots.Count == 0) return;
        var wanted = snapshots.ToDictionary(
            snapshot => (snapshot.TypeName, snapshot.TypeOrdinal), snapshot => snapshot);
        var method = combatState.GetType().GetMethod(
            "IterateHookListeners", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (method?.Invoke(combatState, Array.Empty<object?>()) is not System.Collections.IEnumerable listeners)
            return;
        var ordinals = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var listener in listeners)
        {
            if (listener == null) continue;
            var typeName = listener.GetType().FullName ?? listener.GetType().Name;
            var ordinal = ordinals.GetValueOrDefault(typeName);
            ordinals[typeName] = ordinal + 1;
            if (wanted.TryGetValue((typeName, ordinal), out var snapshot))
                ApplyPrimitiveObjectState(listener, snapshot);
        }
    }

    private static List<object> CaptureCombatHistoryEntryRefs()
    {
        var history = CombatManager.Instance?.History;
        var entriesProperty = history?.GetType().GetProperty(
            "Entries", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        return (entriesProperty?.GetValue(history) as System.Collections.IEnumerable)
            ?.Cast<object?>().Where(entry => entry != null).Cast<object>().ToList()
            ?? new List<object>();
    }

    private sealed class RuntimeCaptureContext
    {
        public required Player Player { get; init; }
        public required CombatState CombatState { get; init; }
        public Dictionary<object, int> ObjectIds { get; } = new(
            System.Collections.Generic.ReferenceEqualityComparer.Instance);
        public Dictionary<object, int> HistoricalMonsterOwnerIds { get; } = new(
            System.Collections.Generic.ReferenceEqualityComparer.Instance);
        public Dictionary<object, int> HistoricalCreatureOwnerIds { get; } = new(
            System.Collections.Generic.ReferenceEqualityComparer.Instance);
        public Dictionary<object, (int OwnerId, string Path)> HistoricalMovePaths { get; } = new(
            System.Collections.Generic.ReferenceEqualityComparer.Instance);
        public Dictionary<object, MonsterModel> CompletedMoveOwners { get; } = new(
            System.Collections.Generic.ReferenceEqualityComparer.Instance);
        public Dictionary<object, Creature> HistoricalMonsterCreatures { get; } = new(
            System.Collections.Generic.ReferenceEqualityComparer.Instance);
        public Dictionary<string, Dictionary<string, int>> NativeMoveStateCounts { get; } = new(
            StringComparer.Ordinal);
        public int NextObjectId { get; set; } = 1;
    }

    private sealed class RuntimeRestoreContext
    {
        public required Player Player { get; init; }
        public required CombatState CombatState { get; init; }
        public required object History { get; init; }
        public Dictionary<int, object> Objects { get; } = new();
    }

    private static List<RuntimeValueSnapshot> CaptureRuntimeValues(
        IEnumerable<object> entries, RuntimeCaptureContext context)
    {
        var entryList = entries.ToList();
        foreach (var entry in entryList)
        {
            if (AnyMember(entry, "Actor") is Creature actor && actor.Monster != null)
            {
                if (context.HistoricalMonsterCreatures.TryGetValue(actor.Monster,
                        out var existingActor) && !ReferenceEquals(existingActor, actor))
                    throw new InvalidOperationException(
                        "Historical monster has conflicting creature owners");
                context.HistoricalMonsterCreatures[actor.Monster] = actor;
            }
            if (entry.GetType().Name != "MonsterPerformedMoveEntry"
                || AnyMember(entry, "Move") is not object move
                || AnyMember(entry, "Monster") is not MonsterModel owner)
                continue;
            if (context.CompletedMoveOwners.TryGetValue(move, out var existing)
                && !ReferenceEquals(existing, owner))
                throw new InvalidOperationException("Historical move has conflicting monster owners");
            context.CompletedMoveOwners[move] = owner;
        }
        return entryList.Select((entry, index) => CaptureRuntimeValue(
            entry, context, 0, $"CombatHistoryEntries[{index}]")).ToList();
    }

    private static List<PowerRuntimeRefsSnapshot> CaptureActivePowerRefs(RuntimeCaptureContext context)
    {
        var creatures = new List<Creature> { context.Player.Creature };
        creatures.AddRange(context.CombatState.Enemies?.Where(enemy => enemy != null)
            ?? Enumerable.Empty<Creature>());
        var result = new List<PowerRuntimeRefsSnapshot>();
        for (var creatureIndex = 0; creatureIndex < creatures.Count; creatureIndex++)
        {
            var powers = creatures[creatureIndex].Powers?.ToList() ?? new List<PowerModel>();
            for (var powerIndex = 0; powerIndex < powers.Count; powerIndex++)
            {
                var power = powers[powerIndex];
                var path = $"ActivePowerRefs[{creatureIndex},{powerIndex}]";
                result.Add(new PowerRuntimeRefsSnapshot
                {
                    CreatureIndex = creatureIndex - 1,
                    PowerIndex = powerIndex,
                    PowerId = power.Id.Entry,
                    Applier = CaptureRuntimeValue(AnyMember(power, "_applier"), context, 0,
                        $"{path}._applier"),
                    Target = CaptureRuntimeValue(AnyMember(power, "_target"), context, 0,
                        $"{path}._target"),
                    InternalData = CaptureRuntimeValue(AnyMember(power, "_internalData"),
                        context, 0, $"{path}._internalData"),
                    DynamicVars = power.DynamicVars.Values.Select(variable =>
                        new PowerDynamicVarSnapshot
                        {
                            Name = variable.Name,
                            State = CapturePrimitiveObjectState(variable)!,
                        }).ToList(),
                });
            }
        }
        return result;
    }

    private static bool IsAttackPresentationCallbackField(FieldInfo field) =>
        field.DeclaringType == typeof(MegaCrit.Sts2.Core.Commands.Builders.AttackCommand)
        && field.Name is "_customAttackerVfxNodes" or "_customHitVfxNodes";

    private static bool IsInactiveHistoricalMoveCallbackField(FieldInfo field) =>
        field.DeclaringType?.Assembly == typeof(PlayCardAction).Assembly
        && ((field.DeclaringType.Name == "MoveState" && field.Name == "_onPerform")
            || (field.DeclaringType.Namespace?.StartsWith(
                    "MegaCrit.Sts2.Core.MonsterMoves.Intents", StringComparison.Ordinal) == true
                && field.Name == "<DamageCalc>k__BackingField")
            || (field.DeclaringType.FullName?.StartsWith(
                    "MegaCrit.Sts2.Core.MonsterMoves.", StringComparison.Ordinal) == true
                && field.Name == "weightLambda"));

    private static bool IsRuntimeSnapshotScalar(Type type) =>
        type.IsEnum || type == typeof(string) || type == typeof(decimal)
        || type == typeof(bool) || type == typeof(char)
        || type == typeof(byte) || type == typeof(sbyte)
        || type == typeof(short) || type == typeof(ushort)
        || type == typeof(int) || type == typeof(uint)
        || type == typeof(long) || type == typeof(ulong)
        || type == typeof(float) || type == typeof(double);

    private static IEnumerable<(string Path, object Value)> HistoricalMoveGraph(MonsterModel monster)
    {
        var machine = monster.MoveStateMachine;
        var queue = new Queue<(string Path, object? Value)>();
        queue.Enqueue(("next", monster.NextMove));
        queue.Enqueue(("current", AnyMember(machine, "_currentState")));
        queue.Enqueue(("initial", AnyMember(machine, "_initialState")));
        if (AnyMember(machine, "States") is System.Collections.IDictionary states)
        {
            foreach (System.Collections.DictionaryEntry entry in states)
                queue.Enqueue(($"states/{entry.Key}", entry.Value));
        }
        if (AnyMember(machine, "StateLog") is System.Collections.IEnumerable log)
        {
            var index = 0;
            foreach (var value in log) queue.Enqueue(($"log/{index++}", value));
        }
        var seen = new HashSet<object>(System.Collections.Generic.ReferenceEqualityComparer.Instance);
        while (queue.Count > 0)
        {
            var (path, value) = queue.Dequeue();
            if (value == null || !seen.Add(value)) continue;
            yield return (path, value);
            queue.Enqueue(($"{path}/followup", AnyMember(value, "FollowUpState")));
            if (AnyMember(value, "States") is System.Collections.IEnumerable branches)
            {
                var index = 0;
                foreach (var branch in branches)
                    queue.Enqueue(($"{path}/branches/{index++}", branch));
            }
        }
    }

    // Dead enemies retained for revival still belong to the combat roster.
    // Only creatures removed from CombatState are historical objects.
    private static List<Creature> SnapshotEnemyRoster(CombatState combatState) =>
        combatState.Enemies?.Where(enemy => enemy != null).ToList()
        ?? new List<Creature>();

    private static int NativeMoveStateCount(RuntimeCaptureContext context,
        MonsterModel owner, string stateId)
    {
        if (!context.NativeMoveStateCounts.TryGetValue(owner.Id.Entry, out var counts))
        {
            var canonical = ModelDb.GetById<MonsterModel>(owner.Id);
            var fresh = canonical?.ToMutable()
                ?? throw new InvalidOperationException(
                    $"Cannot construct native move owner {owner.Id.Entry}");
            fresh.SetUpForCombat();
            counts = HistoricalMoveGraph(fresh)
                .Select(item => (AnyMember(item.Value, "StateId")?.ToString()
                    ?? AnyMember(item.Value, "Id")?.ToString()))
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .GroupBy(id => id!, StringComparer.Ordinal)
                .ToDictionary(group => group.Key, group => group.Count(), StringComparer.Ordinal);
            context.NativeMoveStateCounts[owner.Id.Entry] = counts;
        }
        return counts.GetValueOrDefault(stateId);
    }


    private static RuntimeValueSnapshot CaptureRuntimeValue(
        object? value, RuntimeCaptureContext context, int depth, string path,
        bool historicalInactiveRuntime = false)
    {
        if (value == null)
            return new RuntimeValueSnapshot { Kind = "null" };
        var type = value.GetType();
        var typeName = type.AssemblyQualifiedName ?? type.FullName ?? type.Name;
        if (value is Delegate || type == typeof(IntPtr) || type == typeof(UIntPtr)
            || type.IsPointer || type.IsFunctionPointer)
            throw new InvalidOperationException(
                $"Combat history snapshot cannot capture runtime callback/pointer at {path} "
                + $"(actual type: {type.FullName})");
        if (IsRuntimeSnapshotScalar(type))
        {
            string scalarJson;
            try
            {
                scalarJson = System.Text.Json.JsonSerializer.Serialize(
                    value is Enum ? value.ToString() : value, SnapshotJsonOpts);
            }
            catch (Exception ex) when (ex is NotSupportedException or System.Text.Json.JsonException)
            {
                throw new InvalidOperationException(
                    $"Combat history snapshot cannot serialize scalar at {path} "
                    + $"(actual type: {type.FullName})", ex);
            }
            return new RuntimeValueSnapshot
            {
                Kind = "scalar",
                TypeName = typeName,
                ScalarJson = scalarJson,
            };
        }
        if (value is MegaCrit.Sts2.Core.Combat.History.CombatHistory)
            return new RuntimeValueSnapshot { Kind = "combat_history", TypeName = typeName };
        if (ReferenceEquals(value, context.CombatState))
            return new RuntimeValueSnapshot { Kind = "combat_state", TypeName = typeName };
        if (value is CombatState)
            throw new InvalidOperationException($"Noncanonical combat state in history at {path}");
        if (ReferenceEquals(value, context.Player))
            return new RuntimeValueSnapshot { Kind = "player", TypeName = typeName };
        if (value is Player)
            throw new InvalidOperationException(
                $"Combat history snapshot found a noncanonical player at {path}");
        if (value is PotionModel potion)
        {
            var slots = context.Player.Potions?.ToList() ?? new List<PotionModel>();
            var slot = slots.FindIndex(item => ReferenceEquals(item, potion));
            if (slot >= 0)
                return new RuntimeValueSnapshot
                {
                    Kind = "potion", TypeName = typeName,
                    CardIndex = slot, ModelId = potion.Id.Entry,
                };
            // A consumed potion remains a distinct history object. Serialize
            // its own fields below; its owner resolves to the canonical player.
        }
        if (value is RelicModel relic)
        {
            var relics = context.Player.Relics?.ToList() ?? new List<RelicModel>();
            var index = relics.FindIndex(item => ReferenceEquals(item, relic));
            if (index >= 0)
                return new RuntimeValueSnapshot
                {
                    Kind = "relic", TypeName = typeName,
                    CardIndex = index, ModelId = relic.Id.Entry,
                };
        }
        if (value is Creature creature)
        {
            var enemies = SnapshotEnemyRoster(context.CombatState);
            var enemyIndex = enemies.FindIndex(item => ReferenceEquals(item, creature));
            var isPlayerCreature = ReferenceEquals(context.Player.Creature, creature);
            if (isPlayerCreature || enemyIndex >= 0)
            {
                return new RuntimeValueSnapshot
                {
                    Kind = "creature",
                    TypeName = typeName,
                    IsPlayerCreature = isPlayerCreature,
                    CreatureIndex = enemyIndex >= 0 ? enemyIndex : null,
                    ModelId = creature.Monster?.Id.Entry,
                };
            }
            // A departed enemy remains a distinct history object. Rebuild its
            // native monster and AI without adding it to the active enemy list.
            if (context.ObjectIds.TryGetValue(creature, out var oldId))
                return new RuntimeValueSnapshot { Kind = "ref", TypeName = typeName, RefId = oldId };
            var historicalId = context.NextObjectId++;
            context.ObjectIds[creature] = historicalId;
            context.HistoricalCreatureOwnerIds[creature] = historicalId;
            if (creature.Monster == null)
                throw new InvalidOperationException($"Historical creature has no monster at {path}");
            context.HistoricalMonsterOwnerIds[creature.Monster] = historicalId;
            foreach (var (movePath, moveValue) in HistoricalMoveGraph(creature.Monster))
                context.HistoricalMovePaths.TryAdd(moveValue, (historicalId, movePath));
            var creatureFields = new List<RuntimeFieldSnapshot>();
            for (var current = type; current != null; current = current.BaseType)
            {
                foreach (var field in current.GetFields(BindingFlags.Instance | BindingFlags.Public
                    | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                {
                    // A departed creature is retained only as a history value.
                    // Its event subscriptions target the old combat and cannot
                    // be serialized or invoked after reconstruction. Native
                    // CreateCreature supplies the new object's event fields;
                    // preserve all non-callback state and fail on other unknowns.
                    if (field.IsStatic || typeof(MonsterModel).IsAssignableFrom(field.FieldType)
                        || typeof(Delegate).IsAssignableFrom(field.FieldType)) continue;
                    object? fieldValue;
                    try { fieldValue = field.GetValue(creature); }
                    catch (Exception ex)
                    {
                        throw new InvalidOperationException(
                            $"Cannot capture historical creature field {path}.{field.Name}", ex);
                    }
                    creatureFields.Add(new RuntimeFieldSnapshot
                    {
                        DeclaringType = current.FullName ?? current.Name,
                        Name = field.Name,
                        Value = CaptureRuntimeValue(fieldValue, context, depth + 1,
                            $"{path}.{field.Name}", historicalInactiveRuntime: true),
                    });
                }
            }
            return new RuntimeValueSnapshot
            {
                Kind = "historical_creature", TypeName = typeName, ObjectId = historicalId,
                ModelId = creature.Monster.Id.Entry, SlotName = creature.SlotName,
                HistoricalCreatureStateJson = System.Text.Json.JsonSerializer.Serialize(
                    CaptureEnemyCreatureState(creature), SnapshotJsonOpts),
                HistoricalEnemyAiJson = System.Text.Json.JsonSerializer.Serialize(
                    CaptureEnemyAiStates(new List<Creature> { creature }).Single(), SnapshotJsonOpts),
                HistoricalMonsterRngJson = System.Text.Json.JsonSerializer.Serialize(
                    CaptureDetailedRngState("HistoricalMonster",
                        AnyMember(creature.Monster, "_rng")), SnapshotJsonOpts),
                HistoricalHadMoveStateMachine = creature.Monster.MoveStateMachine != null,
                Fields = creatureFields,
            };
        }
        if (value is MonsterModel monster)
        {
            if (context.HistoricalMonsterOwnerIds.TryGetValue(monster, out var historicalOwnerId))
                return new RuntimeValueSnapshot
                {
                    Kind = "historical_monster", TypeName = typeName, RefId = historicalOwnerId,
                    ModelId = monster.Id.Entry,
                };
            var enemies = SnapshotEnemyRoster(context.CombatState);
            var enemyIndex = enemies.FindIndex(item => ReferenceEquals(item.Monster, monster));
            if (enemyIndex >= 0)
            {
                return new RuntimeValueSnapshot
                {
                    Kind = "monster",
                    TypeName = typeName,
                    CreatureIndex = enemyIndex,
                    ModelId = monster.Id.Entry,
                };
            }
            throw new InvalidOperationException(
                $"Historical monster lacks a canonical or detached creature owner at {path} "
                + $"(id: {monster.Id.Entry})");
        }
        if (string.Equals(type.Name, "MoveState", StringComparison.Ordinal))
        {
            if (context.ObjectIds.TryGetValue(value, out var existingMoveId))
                return new RuntimeValueSnapshot { Kind = "ref", TypeName = typeName,
                    RefId = existingMoveId };
            if (context.HistoricalMovePaths.TryGetValue(value, out var historicalMove))
                return new RuntimeValueSnapshot
                {
                    Kind = "historical_move_state", TypeName = typeName,
                    RefId = historicalMove.OwnerId, MovePath = historicalMove.Path,
                    ModelId = AnyMember(value, "StateId")?.ToString()
                        ?? AnyMember(value, "Id")?.ToString(),
                };
            var stateId = AnyMember(value, "StateId")?.ToString()
                ?? AnyMember(value, "Id")?.ToString();
            var enemies = SnapshotEnemyRoster(context.CombatState);
            for (var enemyIndex = 0; enemyIndex < enemies.Count; enemyIndex++)
            {
                var enemyMonster = enemies[enemyIndex].Monster;
                var stateMachine = enemyMonster?.MoveStateMachine;
                var states = AnyMember(stateMachine, "States") as System.Collections.IDictionary;
                var belongsToEnemy = ReferenceEquals(enemyMonster?.NextMove, value)
                    || ReferenceEquals(AnyMember(stateMachine, "_currentState"), value)
                    || (states?.Values.Cast<object?>().Any(item => ReferenceEquals(item, value)) ?? false)
                    || ((AnyMember(stateMachine, "StateLog") as System.Collections.IEnumerable)
                        ?.Cast<object?>().Any(item => ReferenceEquals(item, value)) ?? false);
                if (!belongsToEnemy) continue;
                return new RuntimeValueSnapshot
                {
                    Kind = "enemy_move_state",
                    TypeName = typeName,
                    CreatureIndex = enemyIndex,
                    ModelId = stateId,
                };
            }
            if (context.CompletedMoveOwners.TryGetValue(value, out var completedOwner)
                && !string.IsNullOrWhiteSpace(stateId)
                && NativeMoveStateCount(context, completedOwner, stateId) == 1)
            {
                var ownerIndex = enemies.FindIndex(enemy =>
                    ReferenceEquals(enemy.Monster, completedOwner));
                var historicalOwnerId = context.HistoricalMonsterOwnerIds
                    .TryGetValue(completedOwner, out var detachedOwnerId)
                        ? detachedOwnerId : (int?)null;
                if ((ownerIndex < 0 && historicalOwnerId == null)
                    || string.IsNullOrWhiteSpace(stateId))
                    throw new InvalidOperationException(
                        $"Completed historical move lacks a native owner at {path}");
                var completedId = context.NextObjectId++;
                context.ObjectIds[value] = completedId;
                return new RuntimeValueSnapshot
                {
                    Kind = "completed_move_state", TypeName = typeName,
                    ObjectId = completedId, CreatureIndex = ownerIndex >= 0 ? ownerIndex : null,
                    RefId = ownerIndex >= 0 ? null : historicalOwnerId,
                    OwnerModelId = completedOwner.Id.Entry, ModelId = stateId,
                    MovePrimitiveState = CapturePrimitiveObjectState(value),
                };
            }
            // A completed historical move that is absent from every active
            // enemy state machine cannot be performed again. Keep its object
            // graph, but omit only the known native AI execution closures.
            historicalInactiveRuntime = true;
        }
        if (value is CardModel card)
        {
            int? pileType = null;
            int? cardIndex = null;
            foreach (var pile in context.Player.PlayerCombatState.AllPiles)
            {
                var index = pile.Cards.ToList().FindIndex(item => ReferenceEquals(item, card));
                if (index < 0) continue;
                pileType = Convert.ToInt32(pile.Type);
                cardIndex = index;
                break;
            }
            var registry = (AnyMember(context.CombatState, "_allCards") as System.Collections.IEnumerable)
                ?.Cast<object?>().OfType<CardModel>().ToList() ?? new List<CardModel>();
            var allCardIndex = registry.FindIndex(item => ReferenceEquals(item, card));
            if (pileType == null)
            {
                // _allCards is rebuilt from active piles during full restore.
                // A card retained only by history must not be reinserted into
                // that registry: doing so changes zone-counting card effects.
                if (context.ObjectIds.TryGetValue(card, out var oldCardId))
                    return new RuntimeValueSnapshot { Kind = "ref", TypeName = typeName, RefId = oldCardId };
                var detachedId = context.NextObjectId++;
                context.ObjectIds[card] = detachedId;
                return new RuntimeValueSnapshot
                {
                    Kind = "historical_card", TypeName = typeName, ObjectId = detachedId,
                    ModelId = card.Id.Entry,
                    NativeJson = System.Text.Json.JsonSerializer.Serialize(card.ToSerializable(), SnapshotJsonOpts),
                };
            }
            return new RuntimeValueSnapshot
            {
                Kind = "card",
                TypeName = typeName,
                PileType = pileType,
                CardIndex = cardIndex,
                AllCardIndex = pileType == null && allCardIndex >= 0 ? allCardIndex : null,
                ModelId = card.Id.Entry,
            };
        }
        if (value is MegaCrit.Sts2.Core.Localization.DynamicVars.CalculatedDamageVar damageVar)
        {
            static bool Matches(CardModel card, MegaCrit.Sts2.Core.Localization.DynamicVars.CalculatedDamageVar candidate)
            {
                try { return ReferenceEquals(card.DynamicVars.CalculatedDamage, candidate); }
                catch (KeyNotFoundException) { return false; }
            }
            // Attack history can retain the card's damage variable through a
            // consumed Vigor power. Rebind to the restored canonical card so
            // its gameplay multiplier callback remains intact.
            foreach (var pile in context.Player.PlayerCombatState.AllPiles)
            {
                var cards = pile.Cards.ToList();
                for (var index = 0; index < cards.Count; index++)
                {
                    if (!Matches(cards[index], damageVar)) continue;
                    return new RuntimeValueSnapshot
                    {
                        Kind = "card_calculated_damage_var", TypeName = typeName,
                        PileType = Convert.ToInt32(pile.Type), CardIndex = index,
                        ModelId = cards[index].Id.Entry,
                    };
                }
            }
            var registry = (AnyMember(context.CombatState, "_allCards") as System.Collections.IEnumerable)
                ?.Cast<object?>().OfType<CardModel>().ToList() ?? new List<CardModel>();
            for (var index = 0; index < registry.Count; index++)
            {
                if (!Matches(registry[index], damageVar)) continue;
                return new RuntimeValueSnapshot
                {
                    Kind = "card_calculated_damage_var", TypeName = typeName,
                    AllCardIndex = index, ModelId = registry[index].Id.Entry,
                };
            }
        }
        if (value is PowerModel power)
        {
            var creatures = new List<Creature> { context.Player.Creature };
            creatures.AddRange(SnapshotEnemyRoster(context.CombatState));
            for (var creatureIndex = 0; creatureIndex < creatures.Count; creatureIndex++)
            {
                var powers = creatures[creatureIndex].Powers?.ToList() ?? new List<PowerModel>();
                var powerIndex = powers.FindIndex(item => ReferenceEquals(item, power));
                if (powerIndex < 0) continue;
                return new RuntimeValueSnapshot
                {
                    Kind = "power",
                    TypeName = typeName,
                    IsPlayerCreature = creatureIndex == 0,
                    CreatureIndex = creatureIndex == 0 ? null : creatureIndex - 1,
                    PowerIndex = powerIndex,
                    ModelId = power.Id.Entry,
                };
            }
            foreach (var (historicalObject, ownerId) in context.HistoricalCreatureOwnerIds)
            {
                if (historicalObject is not Creature departed)
                    continue;
                var powers = departed.Powers?.ToList() ?? new List<PowerModel>();
                var powerIndex = powers.FindIndex(item => ReferenceEquals(item, power));
                if (powerIndex >= 0)
                    return new RuntimeValueSnapshot
                    {
                        Kind = "historical_power", TypeName = typeName,
                        RefId = ownerId, PowerIndex = powerIndex, ModelId = power.Id.Entry,
                    };
            }
        }
        if (context.ObjectIds.TryGetValue(value, out var existingId))
            return new RuntimeValueSnapshot { Kind = "ref", TypeName = typeName, RefId = existingId };
        var objectId = context.NextObjectId++;
        context.ObjectIds[value] = objectId;
        if (depth >= 12)
            throw new InvalidOperationException(
                $"Combat history snapshot exceeded supported depth at {path} "
                + $"(actual type: {type.FullName})");
        if (value is System.Collections.IDictionary dictionary)
        {
            var entries = new List<RuntimeMapEntrySnapshot>();
            foreach (System.Collections.DictionaryEntry entry in dictionary)
            {
                entries.Add(new RuntimeMapEntrySnapshot
                {
                    Key = CaptureRuntimeValue(entry.Key, context, depth + 1,
                        $"{path}.Entries[{entries.Count}].Key", historicalInactiveRuntime),
                    Value = CaptureRuntimeValue(entry.Value, context, depth + 1,
                        $"{path}.Entries[{entries.Count}].Value", historicalInactiveRuntime),
                });
            }
            return new RuntimeValueSnapshot
            {
                Kind = "dictionary", TypeName = typeName, ObjectId = objectId, Entries = entries,
            };
        }
        if (value is System.Collections.IEnumerable enumerable)
        {
            return new RuntimeValueSnapshot
            {
                Kind = "list",
                TypeName = typeName,
                ObjectId = objectId,
                Items = enumerable.Cast<object?>()
                    .Select((item, index) => CaptureRuntimeValue(
                        item, context, depth + 1, $"{path}.Items[{index}]",
                        historicalInactiveRuntime)).ToList(),
            };
        }
        var fields = new List<RuntimeFieldSnapshot>();
        var reflectedFields = new List<FieldInfo>();
        for (var current = type; current != null; current = current.BaseType)
            reflectedFields.AddRange(current.GetFields(
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic |
                BindingFlags.DeclaredOnly));
        // A history entry can name a detached Monster before its base Actor
        // field. Capture the exact Actor first so subsequent native model
        // references point to a materialized historical creature object.
        if (value is MegaCrit.Sts2.Core.Combat.History.CombatHistoryEntry)
            reflectedFields = reflectedFields.OrderBy(field =>
                field.Name == "<Actor>k__BackingField" ? 0 : 1).ToList();
        foreach (var field in reflectedFields)
        {
                // Live move states use the enemy_move_state locator above and
                // retain their native callback. Only detached historical
                // creatures or completed moves omit these known AI closures.
                if (field.IsStatic || IsAttackPresentationCallbackField(field)
                    || (historicalInactiveRuntime && IsInactiveHistoricalMoveCallbackField(field))) continue;
                object? fieldValue;
                try { fieldValue = field.GetValue(value); }
                catch (Exception ex)
                {
                    throw new InvalidOperationException(
                        $"Combat history snapshot cannot read {path}.{field.Name} "
                        + $"(declared type: {field.FieldType.FullName})", ex);
                }
                fields.Add(new RuntimeFieldSnapshot
                {
                    DeclaringType = field.DeclaringType?.FullName ?? field.DeclaringType?.Name
                        ?? type.Name,
                    Name = field.Name,
                    Value = CaptureRuntimeValue(fieldValue, context, depth + 1,
                        $"{path}.{field.Name}", historicalInactiveRuntime),
                });
        }
        return new RuntimeValueSnapshot
        {
            Kind = "object", TypeName = typeName, ObjectId = objectId, Fields = fields,
        };
    }

    private static Type? ResolveRuntimeType(string? typeName)
    {
        if (string.IsNullOrWhiteSpace(typeName)) return null;
        return Type.GetType(typeName, throwOnError: false)
            ?? typeof(PlayCardAction).Assembly.GetType(typeName.Split(',')[0].Trim(), throwOnError: false);
    }

    private static Creature? ResolveRuntimeCreature(RuntimeValueSnapshot snapshot, RuntimeRestoreContext context)
    {
        if (snapshot.IsPlayerCreature) return context.Player.Creature;
        var enemies = SnapshotEnemyRoster(context.CombatState);
        return snapshot.CreatureIndex is int index && index >= 0 && index < enemies.Count
            ? enemies[index] : null;
    }

    private static object? RestoreRuntimeValue(RuntimeValueSnapshot snapshot, RuntimeRestoreContext context)
    {
        if (snapshot.Kind == "null") return null;
        if (snapshot.Kind == "truncated")
            throw new InvalidOperationException("Truncated combat history value cannot be restored");
        if (snapshot.Kind == "ref")
            return snapshot.RefId is int refId && context.Objects.TryGetValue(refId, out var referenced)
                ? referenced : throw new InvalidOperationException(
                    $"Cannot restore combat history reference {snapshot.RefId} ({snapshot.TypeName})");
        if (snapshot.Kind == "completed_move_state")
        {
            var enemies = SnapshotEnemyRoster(context.CombatState);
            var owner = snapshot.CreatureIndex is int ownerIndex && ownerIndex >= 0
                && ownerIndex < enemies.Count ? enemies[ownerIndex]
                : snapshot.RefId is int ownerId
                    && context.Objects.TryGetValue(ownerId, out var historicalOwner)
                    ? historicalOwner as Creature : null;
            if (owner?.Monster?.Id.Entry != snapshot.OwnerModelId
                || snapshot.ObjectId is not int completedId
                || string.IsNullOrWhiteSpace(snapshot.ModelId))
                throw new InvalidOperationException(
                    $"Cannot locate completed move owner {snapshot.OwnerModelId} "
                    + $"at {snapshot.CreatureIndex}/{snapshot.RefId}");
            var canonical = ModelDb.GetById<MonsterModel>(
                new ModelId("MONSTER", snapshot.OwnerModelId));
            var fresh = canonical?.ToMutable()
                ?? throw new InvalidOperationException(
                    $"Cannot construct native move owner {snapshot.OwnerModelId}");
            fresh.SetUpForCombat();
            var matches = HistoricalMoveGraph(fresh)
                .Where(item => (AnyMember(item.Value, "StateId")?.ToString()
                    ?? AnyMember(item.Value, "Id")?.ToString()) == snapshot.ModelId)
                .Select(item => item.Value).Distinct(
                    System.Collections.Generic.ReferenceEqualityComparer.Instance).ToList();
            if (matches.Count != 1)
                throw new InvalidOperationException(
                    $"Native monster {snapshot.OwnerModelId} has {matches.Count} move states "
                    + $"named {snapshot.ModelId}");
            var move = matches[0];
            var primitive = snapshot.MovePrimitiveState
                ?? throw new InvalidOperationException("Completed historical move has no scalar state");
            if (move.GetType().FullName != primitive.TypeName)
                throw new InvalidOperationException(
                    $"Completed historical move type changed: {primitive.TypeName}");
            foreach (var fieldSnapshot in primitive.Fields)
            {
                var declaringType = move.GetType();
                while (declaringType != null && declaringType.FullName != fieldSnapshot.DeclaringType)
                    declaringType = declaringType.BaseType;
                var field = declaringType?.GetField(fieldSnapshot.Name,
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic |
                    BindingFlags.DeclaredOnly);
                if (field == null || field.IsInitOnly
                    || field.FieldType.AssemblyQualifiedName != fieldSnapshot.FieldType)
                    throw new InvalidOperationException(
                        $"Cannot restore completed move field {primitive.TypeName}.{fieldSnapshot.Name}");
                field.SetValue(move, CoerceRelicFieldValue(fieldSnapshot.Value, field.FieldType));
            }
            context.Objects[completedId] = move;
            return move;
        }
        if (snapshot.Kind == "combat_history") return context.History;
        if (snapshot.Kind == "combat_state") return context.CombatState;
        if (snapshot.Kind == "player") return context.Player;
        if (snapshot.Kind == "historical_card")
        {
            var serializable = System.Text.Json.JsonSerializer.Deserialize<
                MegaCrit.Sts2.Core.Saves.Runs.SerializableCard>(snapshot.NativeJson
                    ?? throw new InvalidOperationException("Historical card has no native state"), SnapshotJsonOpts)
                ?? throw new InvalidOperationException("Cannot decode historical card state");
            var card = CardModel.FromSerializable(serializable);
            if (card.Id.Entry != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Historical card identity changed: {snapshot.ModelId} -> {card.Id.Entry}");
            SetField(card, "_owner", context.Player);
            if (snapshot.ObjectId is not int historicalCardId)
                throw new InvalidOperationException($"Historical card {snapshot.ModelId} has no reference identity");
            context.Objects[historicalCardId] = card;
            return card;
        }
        if (snapshot.Kind == "historical_monster")
        {
            if (snapshot.RefId is not int ownerId
                || !context.Objects.TryGetValue(ownerId, out var owner)
                || owner is not Creature historicalOwner
                || historicalOwner.Monster?.Id.Entry != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Cannot rebind historical monster {snapshot.ModelId} to creature {snapshot.RefId}");
            return historicalOwner.Monster;
        }
        if (snapshot.Kind == "historical_power")
        {
            if (snapshot.RefId is not int ownerId
                || !context.Objects.TryGetValue(ownerId, out var owner)
                || owner is not Creature historicalOwner)
                throw new InvalidOperationException(
                    $"Cannot locate historical power owner {snapshot.RefId}");
            var powers = historicalOwner.Powers?.ToList() ?? new List<PowerModel>();
            var index = snapshot.PowerIndex ?? -1;
            if (index < 0 || index >= powers.Count
                || powers[index].Id.Entry != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Cannot rebind historical power {snapshot.ModelId} at {index}");
            return powers[index];
        }
        if (snapshot.Kind == "historical_move_state")
        {
            if (snapshot.RefId is not int ownerId
                || !context.Objects.TryGetValue(ownerId, out var owner)
                || owner is not Creature historicalOwner || historicalOwner.Monster == null)
                throw new InvalidOperationException(
                    $"Cannot locate historical move owner {snapshot.RefId}");
            var move = HistoricalMoveGraph(historicalOwner.Monster)
                .Where(item => item.Path == snapshot.MovePath)
                .Select(item => item.Value).SingleOrDefault();
            var stateId = AnyMember(move, "StateId")?.ToString()
                ?? AnyMember(move, "Id")?.ToString();
            if (move == null || stateId != snapshot.ModelId)
            {
                // Native AI rebuilding may replace a dynamic follow-up edge.
                // The state identifier is unique only within this already
                // located historical monster; never search another creature.
                var matches = HistoricalMoveGraph(historicalOwner.Monster)
                    .Where(item => (AnyMember(item.Value, "StateId")?.ToString()
                        ?? AnyMember(item.Value, "Id")?.ToString()) == snapshot.ModelId)
                    .ToList();
                if (matches.Count == 1) move = matches[0].Value;
                else if (snapshot.MovePath?.Contains('/') == true)
                {
                    var edge = snapshot.MovePath[(snapshot.MovePath.LastIndexOf('/') + 1)..];
                    var sameEdge = matches.Where(item => item.Path.EndsWith($"/{edge}",
                        StringComparison.Ordinal)).ToList();
                    if (sameEdge.Count == 1) move = sameEdge[0].Value;
                }
            }
            if (move == null || (AnyMember(move, "StateId")?.ToString()
                ?? AnyMember(move, "Id")?.ToString()) != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Cannot rebind historical move {snapshot.ModelId} at {snapshot.MovePath}; "
                    + $"available=[{string.Join(",", HistoricalMoveGraph(historicalOwner.Monster)
                        .Select(item => $"{item.Path}:{AnyMember(item.Value, "StateId") ?? AnyMember(item.Value, "Id")}"))}]");
            return move;
        }
        if (snapshot.Kind == "historical_creature")
        {
            var monsterId = snapshot.ModelId
                ?? throw new InvalidOperationException("Historical creature has no monster identity");
            var canonical = ModelDb.GetById<MonsterModel>(new ModelId("MONSTER", monsterId));
            var monster = canonical?.ToMutable()
                ?? throw new InvalidOperationException($"Cannot construct historical monster {monsterId}");
            var creature = context.CombatState.CreateCreature(
                monster, CombatSide.Enemy, snapshot.SlotName ?? $"HISTORY_{snapshot.ObjectId}");
            monster.SetUpForCombat();
            if (snapshot.ObjectId is not int historicalId)
                throw new InvalidOperationException($"Historical creature {monsterId} has no reference identity");
            context.Objects[historicalId] = creature;
            if (snapshot.HistoricalCreatureStateJson != null)
            {
                var creatureState = System.Text.Json.JsonSerializer.Deserialize<
                    CombatSnapshot.EnemyCreatureSnapshot>(
                    snapshot.HistoricalCreatureStateJson, SnapshotJsonOpts)
                    ?? throw new InvalidOperationException(
                        $"Cannot decode historical creature state for {monsterId}");
                if (creatureState.MonsterId != monsterId)
                    throw new InvalidOperationException(
                        $"Historical creature identity changed: {monsterId}");
                ApplyCreatureScalars(creature, creatureState);
                RebuildCreaturePowers(creature, creatureState);
            }
            foreach (var fieldSnapshot in snapshot.Fields
                ?? throw new InvalidOperationException($"Historical creature {monsterId} has no captured fields"))
            {
                var declaringType = creature.GetType();
                while (declaringType != null && declaringType.FullName != fieldSnapshot.DeclaringType)
                    declaringType = declaringType.BaseType;
                var field = declaringType?.GetField(fieldSnapshot.Name,
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
                    | BindingFlags.DeclaredOnly)
                    ?? throw new MissingFieldException(fieldSnapshot.DeclaringType, fieldSnapshot.Name);
                field.SetValue(creature, RestoreRuntimeValue(fieldSnapshot.Value, context));
            }
            if (snapshot.HistoricalEnemyAiJson == null)
                throw new InvalidOperationException($"Historical creature {monsterId} has no AI state");
            var historicalAi = System.Text.Json.JsonSerializer.Deserialize<CombatSnapshot.EnemyAiSnapshot>(
                snapshot.HistoricalEnemyAiJson, SnapshotJsonOpts)
                ?? throw new InvalidOperationException($"Cannot decode historical AI for {monsterId}");
            ApplyEnemyAiState(creature, historicalAi, useCapturedMoveCallbacks: false,
                applySetMoveImmediate: false);
            var historicalMachine = monster.MoveStateMachine;
            if (historicalAi.CurrentStateId == null && historicalMachine != null)
                SetField(historicalMachine, "_currentState", null);
            if (historicalAi.InitialStateId == null && historicalMachine != null)
                SetField(historicalMachine, "_initialState", null);
            if (historicalAi.NextMoveId == null)
                SetField(monster, "<NextMove>k__BackingField", null);
            if (snapshot.HistoricalHadMoveStateMachine == false)
            {
                if (historicalAi.NextMoveId != null)
                {
                    CombatSnapshot.EnemyAiSnapshot.MoveStateSnapshot? savedMove = null;
                    historicalAi.MoveStates?.TryGetValue(historicalAi.NextMoveId, out savedMove);
                    var candidates = HistoricalMoveGraph(monster)
                        .Where(item => (AnyMember(item.Value, "StateId")?.ToString()
                            ?? AnyMember(item.Value, "Id")?.ToString()) == historicalAi.NextMoveId)
                        .Where(item => (AnyMember(AnyMember(item.Value, "FollowUpState"), "StateId")?.ToString()
                            ?? AnyMember(AnyMember(item.Value, "FollowUpState"), "Id")?.ToString())
                            == savedMove?.ResolvedFollowUpStateId)
                        .ToList();
                    var nativeNext = candidates.SingleOrDefault(item => item.Path == "next").Value;
                    if (nativeNext == null && candidates.Count == 1) nativeNext = candidates[0].Value;
                    if (nativeNext == null)
                        throw new InvalidOperationException(
                            $"Cannot reconstruct detached monster {monsterId} next move {historicalAi.NextMoveId} "
                            + $"followup={savedMove?.ResolvedFollowUpStateId}; available=[{string.Join(",",
                                HistoricalMoveGraph(monster)
                                    .Select(item => $"{item.Path}:{AnyMember(item.Value, "StateId") ?? AnyMember(item.Value, "Id")}->{AnyMember(AnyMember(item.Value, "FollowUpState"), "StateId") ?? AnyMember(AnyMember(item.Value, "FollowUpState"), "Id")}"))}]");
                    SetField(monster, "<NextMove>k__BackingField", nativeNext);
                }
                SetField(monster, "_moveStateMachine", null);
            }
            if (snapshot.HistoricalMonsterRngJson != null)
            {
                var historicalRng = System.Text.Json.JsonSerializer.Deserialize<CombatSnapshot.RngStateSnapshot>(
                    snapshot.HistoricalMonsterRngJson, SnapshotJsonOpts);
                ApplyDetailedRngState(historicalRng, AnyMember(monster, "_rng"));
            }
            return creature;
        }
        if (snapshot.Kind == "potion")
        {
            var slots = context.Player.Potions?.ToList() ?? new List<PotionModel>();
            var index = snapshot.CardIndex ?? -1;
            if (index < 0 || index >= slots.Count || slots[index]?.Id.Entry != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Cannot rebind historical potion {snapshot.ModelId} in slot {index}");
            return slots[index];
        }
        if (snapshot.Kind == "relic")
        {
            var relics = context.Player.Relics?.ToList() ?? new List<RelicModel>();
            var index = snapshot.CardIndex ?? -1;
            if (index < 0 || index >= relics.Count || relics[index].Id.Entry != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Cannot rebind historical relic {snapshot.ModelId} at index {index}");
            return relics[index];
        }
        if (snapshot.Kind == "creature")
        {
            var creature = ResolveRuntimeCreature(snapshot, context);
            // Archived envelopes predate creature model IDs. Their exact slot
            // is still checked here; the outer restore validates encounter IDs.
            if (creature == null || (snapshot.ModelId != null
                && creature.Monster?.Id.Entry != snapshot.ModelId))
                throw new InvalidOperationException(
                    $"Cannot rebind historical creature {snapshot.ModelId} at {snapshot.CreatureIndex}");
            return creature;
        }
        if (snapshot.Kind == "monster")
        {
            var monster = ResolveRuntimeCreature(snapshot, context)?.Monster;
            if (monster == null || monster.Id.Entry != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Cannot rebind historical monster {snapshot.ModelId} at {snapshot.CreatureIndex}");
            return monster;
        }
        if (snapshot.Kind == "enemy_move_state")
        {
            var enemy = ResolveRuntimeCreature(snapshot, context);
            var monster = enemy?.Monster;
            var stateMachine = monster?.MoveStateMachine;
            var stateId = snapshot.ModelId;
            var currentState = AnyMember(stateMachine, "_currentState")
                ?? AnyMember(stateMachine, "CurrentState");
            if (string.Equals(
                    AnyMember(currentState, "StateId")?.ToString()
                        ?? AnyMember(currentState, "Id")?.ToString(),
                    stateId,
                    StringComparison.Ordinal))
                return currentState;
            if (string.Equals(
                    AnyMember(monster?.NextMove, "StateId")?.ToString()
                        ?? AnyMember(monster?.NextMove, "Id")?.ToString(),
                    stateId,
                    StringComparison.Ordinal))
                return monster?.NextMove;
            if (AnyMember(stateMachine, "States") is System.Collections.IDictionary states)
            {
                foreach (System.Collections.DictionaryEntry entry in states)
                {
                    if (string.Equals(entry.Key?.ToString(), stateId, StringComparison.Ordinal))
                        return entry.Value;
                }
            }
            if (AnyMember(stateMachine, "StateLog") is System.Collections.IEnumerable stateLog)
            {
                foreach (var state in stateLog)
                {
                    if (string.Equals(
                            AnyMember(state, "StateId")?.ToString()
                                ?? AnyMember(state, "Id")?.ToString(),
                            stateId,
                            StringComparison.Ordinal))
                        return state;
                }
            }
            throw new InvalidOperationException(
                $"Cannot rebind historical move state {stateId} for enemy {snapshot.CreatureIndex}");
        }
        if (snapshot.Kind == "card")
        {
            if (snapshot.PileType is int pileType && snapshot.CardIndex is int cardIndex)
            {
                var pile = context.Player.PlayerCombatState.AllPiles
                    .FirstOrDefault(item => Convert.ToInt32(item.Type) == pileType);
                if (pile != null && cardIndex >= 0 && cardIndex < pile.Cards.Count
                    && pile.Cards[cardIndex].Id.Entry == snapshot.ModelId)
                    return pile.Cards[cardIndex];
            }
            var registry = (AnyMember(context.CombatState, "_allCards") as System.Collections.IEnumerable)
                ?.Cast<object?>().OfType<CardModel>().ToList() ?? new List<CardModel>();
            if (snapshot.PileType == null && snapshot.AllCardIndex is int allIndex
                && allIndex >= 0 && allIndex < registry.Count
                && registry[allIndex].Id.Entry == snapshot.ModelId)
                return registry[allIndex];
            throw new InvalidOperationException(
                $"Cannot rebind historical card {snapshot.ModelId} at pile {snapshot.PileType}, "
                + $"index {snapshot.CardIndex}, registry {snapshot.AllCardIndex}");
        }
        if (snapshot.Kind == "card_calculated_damage_var")
        {
            CardModel? card = null;
            if (snapshot.PileType is int pileType && snapshot.CardIndex is int cardIndex)
            {
                var pile = context.Player.PlayerCombatState.AllPiles
                    .FirstOrDefault(item => Convert.ToInt32(item.Type) == pileType);
                if (pile != null && cardIndex >= 0 && cardIndex < pile.Cards.Count)
                    card = pile.Cards[cardIndex];
            }
            else if (snapshot.AllCardIndex is int allIndex)
            {
                var registry = (AnyMember(context.CombatState, "_allCards") as System.Collections.IEnumerable)
                    ?.Cast<object?>().OfType<CardModel>().ToList() ?? new List<CardModel>();
                if (allIndex >= 0 && allIndex < registry.Count)
                    card = registry[allIndex];
            }
            if (card == null || card.Id.Entry != snapshot.ModelId)
                throw new InvalidOperationException(
                    $"Cannot rebind historical calculated damage variable for card {snapshot.ModelId}");
            return card.DynamicVars.CalculatedDamage;
        }
        if (snapshot.Kind == "power")
        {
            var creature = ResolveRuntimeCreature(snapshot, context);
            var powers = creature?.Powers?.ToList() ?? new List<PowerModel>();
            if (snapshot.PowerIndex is int powerIndex && powerIndex >= 0 && powerIndex < powers.Count
                && powers[powerIndex].Id.Entry == snapshot.ModelId)
                return powers[powerIndex];
            throw new InvalidOperationException(
                $"Cannot rebind historical power {snapshot.ModelId} at {snapshot.PowerIndex}");
        }
        var type = ResolveRuntimeType(snapshot.TypeName);
        if (snapshot.Kind == "scalar")
        {
            if (type == null || snapshot.ScalarJson == null || !IsRuntimeSnapshotScalar(type))
                throw new InvalidOperationException($"Invalid historical scalar {snapshot.TypeName}");
            if (type.IsEnum)
            {
                var enumName = System.Text.Json.JsonSerializer.Deserialize<string>(snapshot.ScalarJson, SnapshotJsonOpts);
                return enumName == null ? null : Enum.Parse(type, enumName);
            }
            return System.Text.Json.JsonSerializer.Deserialize(snapshot.ScalarJson, type, SnapshotJsonOpts);
        }
        if (snapshot.Kind == "list")
        {
            var items = snapshot.Items ?? new List<RuntimeValueSnapshot>();
            if (type == typeof(MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVarSet))
            {
                var vars = new List<MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVar>();
                foreach (var item in items)
                {
                    var pair = RestoreRuntimeValue(item, context)
                        ?? throw new InvalidOperationException("Cannot restore historical dynamic variable entry");
                    var variable = AnyMember(pair, "Value")
                        as MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVar
                        ?? throw new InvalidOperationException("Historical dynamic variable entry has no value");
                    vars.Add(variable);
                }
                var restored = new MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVarSet(vars);
                if (snapshot.ObjectId is int varsId) context.Objects[varsId] = restored;
                return restored;
            }
            if (type?.IsArray == true)
            {
                var elementType = type.GetElementType() ?? typeof(object);
                var array = Array.CreateInstance(elementType, items.Count);
                if (snapshot.ObjectId is int arrayId) context.Objects[arrayId] = array;
                for (var index = 0; index < items.Count; index++)
                    array.SetValue(RestoreRuntimeValue(items[index], context), index);
                return array;
            }
            if (type != null && type.IsGenericType
                && type.GetGenericTypeDefinition() == typeof(HashSet<>))
            {
                // Older and current captures encode every IEnumerable as "list".
                // Rebuild the recorded concrete set type, including its object
                // identity, rather than trying to cast it to IList.
                var set = Activator.CreateInstance(type)
                    ?? throw new InvalidOperationException($"Cannot construct historical set {snapshot.TypeName}");
                if (snapshot.ObjectId is int setId) context.Objects[setId] = set;
                var add = type.GetMethod("Add", new[] { type.GetGenericArguments()[0] })!;
                foreach (var item in items)
                    add.Invoke(set, new[] { RestoreRuntimeValue(item, context) });
                return set;
            }
            var element = type?.GetGenericArguments().FirstOrDefault() ?? typeof(object);
            var listType = type != null && !type.IsInterface && !type.IsAbstract
                ? type : typeof(List<>).MakeGenericType(element);
            var list = Activator.CreateInstance(listType) as System.Collections.IList;
            if (list == null)
                throw new InvalidOperationException($"Cannot construct historical list {snapshot.TypeName}");
            if (snapshot.ObjectId is int listId) context.Objects[listId] = list;
            foreach (var item in items) list.Add(RestoreRuntimeValue(item, context));
            return list;
        }
        if (snapshot.Kind == "dictionary")
        {
            var args = type?.GetGenericArguments() ?? Type.EmptyTypes;
            var dictionaryType = type != null && !type.IsInterface && !type.IsAbstract
                ? type
                : typeof(Dictionary<,>).MakeGenericType(
                    args.Length > 0 ? args[0] : typeof(object),
                    args.Length > 1 ? args[1] : typeof(object));
            var dictionary = Activator.CreateInstance(dictionaryType) as System.Collections.IDictionary;
            if (dictionary == null)
                throw new InvalidOperationException($"Cannot construct historical dictionary {snapshot.TypeName}");
            if (snapshot.ObjectId is int dictionaryId) context.Objects[dictionaryId] = dictionary;
            foreach (var entry in snapshot.Entries ?? new List<RuntimeMapEntrySnapshot>())
                dictionary.Add(
                    RestoreRuntimeValue(entry.Key, context),
                    RestoreRuntimeValue(entry.Value, context));
            return dictionary;
        }
        if (snapshot.Kind != "object" || type == null)
            throw new InvalidOperationException(
                $"Unknown historical value kind/type {snapshot.Kind}/{snapshot.TypeName}");
        object instance;
        try { instance = RuntimeHelpers.GetUninitializedObject(type); }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"Cannot construct historical object {type.FullName}", ex);
        }
        if (snapshot.ObjectId is int objectId) context.Objects[objectId] = instance;
        foreach (var fieldSnapshot in snapshot.Fields ?? new List<RuntimeFieldSnapshot>())
        {
            var declaringType = type;
            while (declaringType != null && declaringType.FullName != fieldSnapshot.DeclaringType)
                declaringType = declaringType.BaseType;
            var field = declaringType?.GetField(
                fieldSnapshot.Name,
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic |
                BindingFlags.DeclaredOnly);
            if (field == null)
                throw new InvalidOperationException(
                    $"Missing historical field {fieldSnapshot.DeclaringType}.{fieldSnapshot.Name}");
            if (IsAttackPresentationCallbackField(field)
                || IsInactiveHistoricalMoveCallbackField(field)) continue;
            try { field.SetValue(instance, RestoreRuntimeValue(fieldSnapshot.Value, context)); }
            catch (Exception ex)
            {
                throw new InvalidOperationException(
                    $"Cannot restore combat history field {type.FullName}.{fieldSnapshot.Name}", ex);
            }
        }
        if (instance is MegaCrit.Sts2.Core.Commands.Builders.AttackCommand)
        {
            // History retains completed attacks for gameplay queries. Their
            // VFX factories are presentation-only and must be valid empty
            // lists even when the attack was allocated without a constructor.
            foreach (var fieldName in new[] { "_customAttackerVfxNodes", "_customHitVfxNodes" })
            {
                var field = typeof(MegaCrit.Sts2.Core.Commands.Builders.AttackCommand).GetField(fieldName,
                    BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.DeclaredOnly)
                    ?? throw new MissingFieldException(type.FullName, fieldName);
                field.SetValue(instance, Activator.CreateInstance(field.FieldType)
                    ?? throw new InvalidOperationException($"Cannot create empty {fieldName} for restored attack"));
            }
        }
        return instance;
    }

    private static void RestoreCombatHistoryForSnapshot(
        CombatSnapshot snapshot, Player player, CombatState combatState)
    {
        var history = CombatManager.Instance?.History;
        if (history == null)
            throw new InvalidOperationException("Combat history is unavailable during restore");
        var entriesField = history.GetType().GetField(
            "_entries", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (entriesField?.GetValue(history) is not System.Collections.IList entries)
            throw new InvalidOperationException("Combat history entry list is unavailable during restore");
        entries.Clear();
        if (snapshot.CombatHistoryEntries != null)
        {
            var context = new RuntimeRestoreContext
            {
                Player = player, CombatState = combatState, History = history,
            };
            foreach (var entrySnapshot in snapshot.CombatHistoryEntries)
            {
                var entry = RestoreRuntimeValue(entrySnapshot, context);
                if (entry == null)
                    throw new InvalidOperationException("Combat history entry restored as null");
                entries.Add(entry);
            }
            RestoreActivePowerRefs(snapshot.ActivePowerRefs, context);
            return;
        }
        if (snapshot.CombatHistoryEntryRefs == null) return;
        foreach (var entry in snapshot.CombatHistoryEntryRefs)
        {
            SetField(entry, "<History>k__BackingField", history);
            entries.Add(entry);
        }
    }

    private static void RestoreActivePowerRefs(
        List<PowerRuntimeRefsSnapshot>? snapshots, RuntimeRestoreContext context)
    {
        if (snapshots == null) return; // Archived envelopes predate this field.
        var enemies = context.CombatState.Enemies?.Where(enemy => enemy != null)
            .ToList() ?? new List<Creature>();
        foreach (var snapshot in snapshots)
        {
            var creature = snapshot.CreatureIndex == -1 ? context.Player.Creature
                : snapshot.CreatureIndex >= 0 && snapshot.CreatureIndex < enemies.Count
                    ? enemies[snapshot.CreatureIndex] : null;
            var powers = creature?.Powers?.ToList() ?? new List<PowerModel>();
            if (snapshot.PowerIndex < 0 || snapshot.PowerIndex >= powers.Count
                || powers[snapshot.PowerIndex].Id.Entry != snapshot.PowerId)
                throw new InvalidOperationException(
                    $"Cannot rebind active power {snapshot.PowerId} at creature "
                    + $"{snapshot.CreatureIndex}, index {snapshot.PowerIndex}");
            var power = powers[snapshot.PowerIndex];
            var applier = RestoreRuntimeValue(snapshot.Applier, context);
            var target = RestoreRuntimeValue(snapshot.Target, context);
            if (applier is not null and not Creature || target is not null and not Creature)
                throw new InvalidOperationException(
                    $"Invalid creature reference on active power {snapshot.PowerId}");
            SetField(power, "_applier", applier);
            SetField(power, "_target", target);
            if (snapshot.InternalData != null)
                SetField(power, "_internalData",
                    RestoreRuntimeValue(snapshot.InternalData, context));
            if (snapshot.DynamicVars != null)
            {
                var variables = power.DynamicVars.Values.ToDictionary(variable => variable.Name,
                    StringComparer.Ordinal);
                if (variables.Count != snapshot.DynamicVars.Count)
                    throw new InvalidOperationException($"Dynamic variable count differs for {snapshot.PowerId}");
                foreach (var variableSnapshot in snapshot.DynamicVars)
                {
                    if (!variables.TryGetValue(variableSnapshot.Name, out var variable)
                        || variable.GetType().FullName != variableSnapshot.State.TypeName)
                        throw new InvalidOperationException(
                            $"Cannot bind dynamic variable {snapshot.PowerId}.{variableSnapshot.Name}");
                    foreach (var fieldSnapshot in variableSnapshot.State.Fields)
                    {
                        var declaringType = variable.GetType();
                        while (declaringType != null && declaringType.FullName != fieldSnapshot.DeclaringType)
                            declaringType = declaringType.BaseType;
                        var field = declaringType?.GetField(fieldSnapshot.Name,
                            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic |
                            BindingFlags.DeclaredOnly);
                        if (field == null || field.IsInitOnly ||
                            field.FieldType.AssemblyQualifiedName != fieldSnapshot.FieldType)
                            throw new InvalidOperationException(
                                $"Cannot restore {snapshot.PowerId}.{variableSnapshot.Name}.{fieldSnapshot.Name}");
                        field.SetValue(variable, CoerceRelicFieldValue(fieldSnapshot.Value, field.FieldType));
                    }
                }
            }
        }
    }

    private static void RemoveNetCombatCardDbSubscriptions(object instance, Type dbType)
    {
        var subscriptions = dbType.GetField(
            "_subscriptions", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?.GetValue(instance) as System.Collections.IList;
        if (subscriptions == null) return;
        foreach (var subscription in subscriptions.Cast<object?>().Where(item => item != null).ToList())
        {
            var type = subscription!.GetType();
            var pile = type.GetField("pile", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                ?.GetValue(subscription);
            var action = type.GetField("action", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                ?.GetValue(subscription) as Delegate;
            if (pile == null || action == null) continue;
            var eventField = FindEventBackingField(pile.GetType(), "ContentsChanged");
            if (eventField?.GetValue(pile) is Delegate current)
                eventField.SetValue(pile, Delegate.Remove(current, action));
        }
        subscriptions.Clear();
    }

    private void RestoreCombatCardDb()
    {
        var dbType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb");
        var instance = dbType?.GetProperty("Instance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)?.GetValue(null);
        if (instance == null || _runState == null)
            return;

        RemoveNetCombatCardDbSubscriptions(instance, dbType!);
        dbType?.GetMethod("ClearCardsForTesting", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            ?.Invoke(instance, Array.Empty<object?>());
        dbType?.GetMethod("StartCombat", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            ?.Invoke(instance, new object?[] { _runState.Players });

        var currentPlayer = _runState.Players.FirstOrDefault();
        var playerCombatState = currentPlayer?.PlayerCombatState;
        if (playerCombatState == null)
            return;

        foreach (var pile in playerCombatState.AllPiles)
        {
            foreach (var card in pile.Cards)
            {
                dbType?.GetMethod("IdCardIfNecessary", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                    ?.Invoke(instance, new object?[] { card });
            }
        }
    }

    private void RestoreCombatCardRuntime(CombatState combatState, PlayerCombatState playerCombatState)
    {
        static IEnumerable<CardModel> EnumerateCards(CombatState combatState, PlayerCombatState playerCombatState)
        {
            var seen = new HashSet<CardModel>(ReferenceEqualityComparerT<CardModel>.Instance);
            foreach (var pile in playerCombatState.AllPiles)
            {
                foreach (var card in pile.Cards)
                {
                    if (seen.Add(card))
                        yield return card;
                }
            }

            if (AnyMember(combatState, "_allCards") is System.Collections.IEnumerable allCards)
            {
                foreach (var item in allCards)
                {
                    if (item is CardModel card && seen.Add(card))
                        yield return card;
                }
            }
        }

        var nonPublic = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        var energyCostType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Entities.Cards.CardEnergyCost");
        var energyCostCtor = energyCostType?.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: new[] { typeof(CardModel), typeof(int), typeof(bool) },
            modifiers: null);

        foreach (var card in EnumerateCards(combatState, playerCombatState))
        {
            // Force the official star-cost lazy path first.
            var baseStarCostProp = card.GetType().GetProperty("BaseStarCost", nonPublic);
            object? baseStarCost = null;
            try { baseStarCost = baseStarCostProp?.GetValue(card); } catch { }
            if (baseStarCost is int bs && AnyMember(card, "_starCostSet") is bool set && !set)
            {
                SetField(card, "_baseStarCost", bs);
                SetField(card, "_starCostSet", true);
            }

            // Ensure every combat card has a runtime energy-cost object, not just cards that were
            // touched by hand evaluation.
            if (AnyMember(card, "_energyCost") == null)
            {
                var canonicalEnergy = 0;
                var hasEnergyX = false;
                try { canonicalEnergy = Convert.ToInt32(card.GetType().GetProperty("CanonicalEnergyCost", nonPublic)?.GetValue(card) ?? 0); } catch { }
                try { hasEnergyX = Convert.ToBoolean(card.GetType().GetProperty("HasEnergyCostX", nonPublic)?.GetValue(card) ?? false); } catch { }

                if (energyCostCtor != null)
                {
                    try
                    {
                        var energyCost = energyCostCtor.Invoke(new object?[] { card, canonicalEnergy, hasEnergyX });
                        SetField(card, "_energyCost", energyCost);
                        var mockSet = card.GetType().GetMethod("MockSetEnergyCost", nonPublic);
                        try { mockSet?.Invoke(card, new[] { energyCost }); } catch { }
                    }
                    catch
                    {
                        // Fallback: force property getter in case runtime lazy init is sufficient.
                        try { _ = card.GetType().GetProperty("EnergyCost", nonPublic)?.GetValue(card); } catch { }
                    }
                }
                else
                {
                    try { _ = card.GetType().GetProperty("EnergyCost", nonPublic)?.GetValue(card); } catch { }
                }
            }

            // Some restored cards still keep their star-cost cache in the default non-combat state.
            // Mirror the live combat shape seen in validated states.
            if (AnyMember(card, "_starCostSet") is bool starCostSet && !starCostSet)
            {
                SetField(card, "_baseStarCost", baseStarCost is int baseStar ? baseStar : -1);
                SetField(card, "_starCostSet", true);
            }
        }
    }

    private static List<CombatSnapshot.EnemyAiSnapshot> CaptureEnemyAiStates(IReadOnlyList<Creature> enemies)
    {
        var snapshots = new List<CombatSnapshot.EnemyAiSnapshot>();
        foreach (var enemy in enemies)
        {
            var monster = enemy.Monster;
            var sm = monster?.MoveStateMachine;
            var states = AnyMember(sm, "States") as System.Collections.IDictionary;
            var reverse = new Dictionary<object, string>(ReferenceEqualityComparer.Instance);
            if (states != null)
            {
                foreach (System.Collections.DictionaryEntry entry in states)
                {
                    if (entry.Value != null)
                        reverse[entry.Value] = entry.Key?.ToString() ?? "";
                }
            }

            string? StateIdOf(object? obj)
            {
                if (obj == null) return null;
                if (reverse.TryGetValue(obj, out var id) && !string.IsNullOrWhiteSpace(id))
                    return id;
                return AnyMember(obj, "Id")?.ToString() ?? AnyMember(obj, "StateId")?.ToString();
            }

            var stateLogIds = new List<string>();
            var stateLogObjects = new List<object>();
            if (AnyMember(sm, "StateLog") is System.Collections.IEnumerable stateLog)
            {
                foreach (var item in stateLog)
                {
                    var id = StateIdOf(item);
                    if (!string.IsNullOrWhiteSpace(id))
                        stateLogIds.Add(id!);
                    if (item != null)
                        stateLogObjects.Add(item);
                }
            }

            var currentStateObj = AnyMember(sm, "_currentState") ?? AnyMember(sm, "CurrentState");
            var initialStateObj = AnyMember(sm, "_initialState") ?? AnyMember(sm, "InitialState");
            var nextMoveObj = AnyMember(monster, "NextMove");
            var moveStates = new Dictionary<string, CombatSnapshot.EnemyAiSnapshot.MoveStateSnapshot>(StringComparer.Ordinal);

            void CaptureMoveState(object? state)
            {
                var stateId = StateIdOf(state);
                if (state == null || string.IsNullOrWhiteSpace(stateId) || moveStates.ContainsKey(stateId!))
                    return;

                var intents = (AnyMember(state, "Intents") as System.Collections.IEnumerable)
                    ?.Cast<object?>()
                    .Where(intent => intent != null)
                    .Cast<object>()
                    .ToList() ?? new List<object>();
                var followUpState = AnyMember(state, "FollowUpState");
                var perform = AnyMember(state, "_onPerform") as Delegate;
                var performPower = perform?.Target as PowerModel;

                moveStates[stateId!] = new CombatSnapshot.EnemyAiSnapshot.MoveStateSnapshot
                {
                    StateId = stateId!,
                    FollowUpStateId = AnyMember(state, "FollowUpStateId")?.ToString(),
                    ResolvedFollowUpStateId = StateIdOf(followUpState),
                    MustPerformOnceBeforeTransitioning =
                        AnyMember(state, "MustPerformOnceBeforeTransitioning") is bool mustPerform && mustPerform,
                    PerformedAtLeastOnce =
                        AnyMember(state, "_performedAtLeastOnce") is bool performed && performed,
                    IntentTypeNames = intents
                        .Select(intent => intent.GetType().FullName ?? intent.GetType().Name)
                        .ToList(),
                    PerformOwnerPowerId = performPower?.Id.Entry,
                    PerformMethodName = performPower != null ? perform?.Method.Name : null,
                    PerformDeclaringType = performPower != null ? perform?.Method.DeclaringType?.FullName : null,
                    OnPerformRef = perform,
                    IntentRefs = intents,
                };

                // Runtime-only successors are not guaranteed to be present in
                // the static States dictionary or state log. Capture their
                // definitions while the live object graph is still available.
                CaptureMoveState(followUpState);
            }

            CaptureMoveState(currentStateObj);
            CaptureMoveState(initialStateObj);
            CaptureMoveState(nextMoveObj);
            foreach (var state in stateLogObjects)
                CaptureMoveState(state);

            snapshots.Add(new CombatSnapshot.EnemyAiSnapshot
            {
                MonsterId = enemy.Monster?.Id.Entry ?? "",
                CurrentStateId = StateIdOf(currentStateObj),
                InitialStateId = StateIdOf(initialStateObj),
                NextMoveId = nextMoveObj != null ? StateIdOf(nextMoveObj) : null,
                PerformedFirstMove = AnyMember(sm, "_performedFirstMove") is bool pfm ? pfm : null,
                StateLogIds = stateLogIds,
                MoveStates = moveStates,
            });
        }
        return snapshots;
    }

    private static List<RelicStateSnapshot> CaptureRelicStates(Player player)
    {
        // Capture the primitive instance fields of each relic so a restore can
        // write them back. Relics hold per-combat trigger flags (e.g.
        // Vambrace._blockGainedThisCombat) that live outside the serialized combat
        // state; without round-tripping them, restoring a snapshot would leave the
        // relic in whatever state the live combat last left it. Only mutable
        // primitive/enum/string fields are captured — complex object refs are
        // skipped (resetting the flag is what fixes the leak; the engine re-derives
        // the ref on its next trigger). Keyed by id + field name so it survives the
        // cross-process JSON envelope.
        var states = new List<RelicStateSnapshot>();
        foreach (var relic in player.Relics ?? Enumerable.Empty<RelicModel>())
        {
            if (relic == null) continue;
            var fields = new Dictionary<string, object?>();
            var nullRefs = new List<string>();
            foreach (var f in relic.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
            {
                if (f.IsInitOnly) continue;
                var t = f.FieldType;
                if (t.IsPrimitive || t.IsEnum || t == typeof(string) || t == typeof(decimal))
                {
                    object? v = null;
                    try { v = f.GetValue(relic); } catch { continue; }
                    if (v is Enum) v = v.ToString();
                    fields[f.Name] = v;
                }
                else if (!t.IsValueType)
                {
                    // Reference-type combat-scoped field: record if it was null so
                    // restore can clear a stale ref left by a prior search line.
                    object? v = null;
                    try { v = f.GetValue(relic); } catch { continue; }
                    if (v == null) nullRefs.Add(f.Name);
                }
            }
            if (fields.Count > 0 || nullRefs.Count > 0)
                states.Add(new RelicStateSnapshot { Id = relic.Id.Entry, Fields = fields, NullRefFields = nullRefs.Count > 0 ? nullRefs : null });
        }
        return states;
    }

    // Restore the player's potion slots during in_place restore. The data path
    // (ApplyCapturedCombatSnapshot) repairs creature/relic/pile state but did NOT
    // touch _potionSlots, so a churned worker whose prior replayed line consumed
    // a potion kept that consumption — leaving the search blind to potion actions
    // (measured: the last residual diff vs a clean restore was exactly the 3
    // potion use/discard actions going missing). Rebuild the slots from the
    // snapshot's serializable player, mirroring the set_player path.
    private void RestorePotionSlots(Player player, CombatSnapshot snapshot)
    {
        try
        {
            var serPotions = snapshot.Player?.Potions;
            if (serPotions == null)
                return;
            var slots = GetBackingList<PotionModel>(player, "_potionSlots")
                     ?? GetBackingList<PotionModel?>(player, "_potionSlots") as System.Collections.IList;
            if (slots == null)
                return;
            // Snapshot of current contents to compare; only rebuild if the live
            // slots diverge from the snapshot (avoids disturbing the normal case).
            var want = new Dictionary<int, string>();
            foreach (var sp in serPotions)
            {
                var entry = sp?.Id?.Entry;
                if (!string.IsNullOrEmpty(entry) && sp.SlotIndex >= 0 && sp.SlotIndex < slots.Count)
                    want[sp.SlotIndex] = entry;
            }
            bool matches = true;
            for (int i = 0; i < slots.Count; i++)
            {
                var liveId = (slots[i] as PotionModel)?.Id.Entry;
                want.TryGetValue(i, out var wantId);
                if (liveId != wantId) { matches = false; break; }
            }
            if (matches)
                return; // live potions already correct — leave them (normal path)

            // Diverged (churn consumed/changed potions): clear and re-add via the
            // canonical AddPotionInternal, which sets potion.Owner = player so the
            // potion registers as a combat hook listener (CombatState.Contains
            // walks owners; a slot-filled potion with null Owner NPEs there).
            for (int i = 0; i < slots.Count; i++)
                slots[i] = null;
            foreach (var kv in want)
            {
                var model = ModelDb.GetById<PotionModel>(new ModelId("POTION", kv.Value));
                var mutable = model?.ToMutable();
                if (mutable == null)
                    continue;
                player.AddPotionInternal(mutable, kv.Key, silent: true);
            }
        }
        catch (Exception ex)
        {
            Log($"RestorePotionSlots failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private void ApplyRelicStates(Player player, List<RelicStateSnapshot>? relicStates)
    {
        if (relicStates == null || relicStates.Count == 0) return;
        var byId = new Dictionary<string, RelicModel>(StringComparer.Ordinal);
        foreach (var relic in player.Relics ?? Enumerable.Empty<RelicModel>())
        {
            if (relic != null) byId[relic.Id.Entry] = relic;
        }
        foreach (var snap in relicStates)
        {
            if (!byId.TryGetValue(snap.Id, out var relic)) continue;
            foreach (var (name, value) in snap.Fields)
            {
                var field = relic.GetType().GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (field == null || field.IsInitOnly) continue;
                try
                {
                    object? coerced = CoerceRelicFieldValue(value, field.FieldType);
                    field.SetValue(relic, coerced);
                }
                catch { }
            }
            if (snap.NullRefFields != null)
            {
                foreach (var name in snap.NullRefFields)
                {
                    var field = relic.GetType().GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    if (field == null || field.IsInitOnly || field.FieldType.IsValueType) continue;
                    try { field.SetValue(relic, null); } catch { }
                }
            }
        }
    }

    // Coerce a captured relic field value to the live field's type. Same-process
    // restore hands back the original boxed primitive; cross-process restore hands
    // back a JsonElement that must be unwrapped first.
    private static object? CoerceRelicFieldValue(object? value, Type targetType)
    {
        if (value is System.Text.Json.JsonElement je)
        {
            switch (je.ValueKind)
            {
                case System.Text.Json.JsonValueKind.True:
                case System.Text.Json.JsonValueKind.False:
                    value = je.GetBoolean();
                    break;
                case System.Text.Json.JsonValueKind.String:
                    value = je.GetString();
                    break;
                case System.Text.Json.JsonValueKind.Number:
                    value = je.TryGetInt64(out var l) ? l : je.GetDouble();
                    break;
                case System.Text.Json.JsonValueKind.Null:
                case System.Text.Json.JsonValueKind.Undefined:
                    return null;
                default:
                    return null;
            }
        }
        if (value == null) return null;
        if (targetType.IsEnum)
            return value is string es ? Enum.Parse(targetType, es) : Enum.ToObject(targetType, value);
        if (targetType == value.GetType()) return value;
        if (targetType.IsPrimitive || targetType == typeof(decimal))
            return Convert.ChangeType(value, targetType);
        return value;
    }

    private static List<CombatSnapshot.EnemyCreatureSnapshot> CaptureEnemyCreatureStates(IReadOnlyList<Creature> enemies)
    {
        var snapshots = new List<CombatSnapshot.EnemyCreatureSnapshot>();
        foreach (var enemy in enemies)
        {
            if (enemy == null)
                continue;
            snapshots.Add(CaptureEnemyCreatureState(enemy));
        }

        return snapshots;
    }

    private static CombatSnapshot.EnemyCreatureSnapshot CaptureEnemyCreatureState(Creature enemy) => new()
    {
        MonsterId = enemy.Monster?.Id.Entry ?? enemy.Name ?? "UNKNOWN",
        SlotName = enemy.SlotName,
        CurrentHp = enemy.CurrentHp,
        MaxHp = enemy.MaxHp,
        Block = enemy.Block,
        MonsterMaxHpBeforeModification = AnyMember(
            enemy, "MonsterMaxHpBeforeModification") as int?,
        CombatId = AnyMember(enemy, "CombatId") as uint?,
        SpawnedThisTurn = AnyMember(enemy.Monster, "_spawnedThisTurn") as bool?,
        MonsterRng = CaptureDetailedRngState(
            "Monster", AnyMember(enemy.Monster, "_rng")),
        Powers = enemy.Powers
            .Select(p => new CombatSnapshot.EnemyPowerSnapshot
            {
                Id = p.Id.Entry,
                Amount = p.Amount,
                AmountOnTurnStart = AnyMember(p, "_amountOnTurnStart") as int?,
            })
            .ToList(),
    };

    private static void ApplyEnemyAiState(
        Creature enemy,
        CombatSnapshot.EnemyAiSnapshot snapshot,
        bool useCapturedMoveCallbacks,
        bool applySetMoveImmediate = true)
    {
        var monster = enemy.Monster;
        var sm = monster?.MoveStateMachine;
        var states = AnyMember(sm, "States") as System.Collections.IDictionary;
        if (monster == null || sm == null || states == null)
            return;

        object? ResolveState(string? stateId)
        {
            if (string.IsNullOrWhiteSpace(stateId))
                return null;
            foreach (System.Collections.DictionaryEntry entry in states)
            {
                if (string.Equals(entry.Key?.ToString(), stateId, StringComparison.Ordinal))
                    return entry.Value;
            }
            // The States dictionary is not the complete native move graph.
            // Initial/branch nodes can live only on the machine's initial or
            // current edge (or a follow-up). Rebind them from this fresh
            // monster's graph; never transplant a callback from another owner.
            var matches = HistoricalMoveGraph(monster)
                .Where(item => string.Equals(
                    AnyMember(item.Value, "StateId")?.ToString()
                        ?? AnyMember(item.Value, "Id")?.ToString(),
                    stateId, StringComparison.Ordinal))
                .Select(item => item.Value)
                .Distinct(System.Collections.Generic.ReferenceEqualityComparer.Instance)
                .ToList();
            if (matches.Count > 1)
                throw new InvalidOperationException(
                    $"Ambiguous native move state {stateId} for {monster.Id.Entry}");
            return matches.SingleOrDefault();
        }

        var moveStateCache = new Dictionary<string, object>(StringComparer.Ordinal);

        void ReplaceStateTemplate(string stateId, object rebuilt)
        {
            object? matchingKey = null;
            foreach (System.Collections.DictionaryEntry entry in states)
            {
                if (string.Equals(entry.Key?.ToString(), stateId, StringComparison.Ordinal))
                {
                    matchingKey = entry.Key;
                    break;
                }
            }
            if (matchingKey != null)
                states[matchingKey] = rebuilt;
        }

        object? ResolveSnapshotState(string? stateId)
        {
            if (string.IsNullOrWhiteSpace(stateId))
                return null;
            if (moveStateCache.TryGetValue(stateId, out var cached))
                return cached;

            var staticTemplate = ResolveState(stateId);
            CombatSnapshot.EnemyAiSnapshot.MoveStateSnapshot? moveSnapshot = null;
            snapshot.MoveStates?.TryGetValue(stateId, out moveSnapshot);

            // Backward compatibility for archived snapshots captured before
            // MoveStates was serialized. STUNNED is the runtime-only state that
            // triggered the recovery storm; CreatureCmd.Stun's default callback
            // is a no-op, and its follow-up is the last state-log entry.
            if (moveSnapshot == null && string.Equals(stateId, "STUNNED", StringComparison.Ordinal))
            {
                moveSnapshot = new CombatSnapshot.EnemyAiSnapshot.MoveStateSnapshot
                {
                    StateId = "STUNNED",
                    FollowUpStateId = snapshot.StateLogIds.LastOrDefault(),
                    MustPerformOnceBeforeTransitioning = true,
                    PerformedAtLeastOnce = false,
                    IntentTypeNames = new List<string>
                    {
                        "MegaCrit.Sts2.Core.MonsterMoves.Intents.StunIntent",
                    },
                };
            }

            if (moveSnapshot == null)
                return staticTemplate;

            var rebuilt = RebuildMoveState(moveSnapshot, staticTemplate, enemy, useCapturedMoveCallbacks);
            moveStateCache[stateId] = rebuilt;
            if (AnyMember(rebuilt, "IsMove") is bool isMove && isMove)
                ReplaceStateTemplate(stateId, rebuilt);
            if (!string.IsNullOrWhiteSpace(moveSnapshot.ResolvedFollowUpStateId))
            {
                var resolvedFollowUp = ResolveSnapshotState(moveSnapshot.ResolvedFollowUpStateId);
                SetPropertyOrField(
                    rebuilt,
                    "FollowUpState",
                    "<FollowUpState>k__BackingField",
                    resolvedFollowUp);
            }
            return rebuilt;
        }

        var currentState = ResolveSnapshotState(snapshot.CurrentStateId);
        var initialState = ResolveSnapshotState(snapshot.InitialStateId);
        var nextMove = ResolveSnapshotState(snapshot.NextMoveId);

        if (currentState != null)
            SetField(sm, "_currentState", currentState);
        if (initialState != null)
            SetField(sm, "_initialState", initialState);
        if (snapshot.PerformedFirstMove.HasValue)
            SetField(sm, "_performedFirstMove", snapshot.PerformedFirstMove.Value);
        if (nextMove != null)
        {
            SetField(monster, "<NextMove>k__BackingField", nextMove);
            try
            {
                if (applySetMoveImmediate)
                    monster.GetType()
                        .GetMethod("SetMoveImmediate", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?
                        .Invoke(monster, new object?[] { nextMove, false });
            }
            catch { }
        }

        if (AnyMember(sm, "StateLog") is System.Collections.IList stateLog)
        {
            stateLog.Clear();
            foreach (var stateId in snapshot.StateLogIds)
            {
                var state = ResolveSnapshotState(stateId);
                if (state != null)
                    stateLog.Add(state);
            }
        }
    }

    private static object RebuildMoveState(
        CombatSnapshot.EnemyAiSnapshot.MoveStateSnapshot snapshot,
        object? staticTemplate,
        Creature owner,
        bool useCapturedMoveCallbacks)
    {
        // Branch states such as INIT_MOVE and RAND are immutable templates, not
        // executable MoveState instances. Reusing those templates is exact; only
        // move states carry the mutable perform callback/flags rebuilt below.
        if (staticTemplate != null && AnyMember(staticTemplate, "IsMove") is bool isMove && !isMove)
            return staticTemplate;

        var moveStateType = typeof(CombatManager).Assembly.GetType(
            "MegaCrit.Sts2.Core.MonsterMoves.MonsterMoveStateMachine.MoveState",
            throwOnError: true)!;
        var ctor = moveStateType.GetConstructors(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .FirstOrDefault(candidate =>
            {
                var parameters = candidate.GetParameters();
                return parameters.Length == 3
                    && parameters[0].ParameterType == typeof(string)
                    && parameters[2].ParameterType.IsArray;
            }) ?? throw new InvalidOperationException("MoveState constructor unavailable during restore");
        var parameters = ctor.GetParameters();

        // Static move callbacks are usually instance delegates bound to the
        // current MonsterModel. A snapshot may survive a full restore that
        // replaces that model, so reusing its captured delegate would execute
        // against a detached monster whose CombatState is null. Always prefer
        // the current monster's static template; captured refs are only valid
        // for runtime-only states that have no static template (e.g. STUNNED).
        object? callback = AnyMember(staticTemplate, "_onPerform");
        if (callback != null && snapshot.OnPerformRef == null)
            snapshot.OnPerformRef = callback;
        if (callback == null && !string.IsNullOrWhiteSpace(snapshot.PerformOwnerPowerId))
        {
            var powers = owner.Powers.Where(power =>
                power.Id.Entry == snapshot.PerformOwnerPowerId).ToList();
            if (powers.Count != 1 || string.IsNullOrWhiteSpace(snapshot.PerformMethodName)
                || string.IsNullOrWhiteSpace(snapshot.PerformDeclaringType))
                throw new InvalidOperationException(
                    $"Cannot rebind power move {snapshot.StateId} to {snapshot.PerformOwnerPowerId}");
            var methods = powers[0].GetType()
                .GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                .Where(method => method.Name == snapshot.PerformMethodName
                    && method.DeclaringType?.FullName == snapshot.PerformDeclaringType
                    && method.ReturnType == typeof(Task)
                    && method.GetParameters().Length == 1
                    && method.GetParameters()[0].ParameterType == typeof(IReadOnlyList<Creature>))
                .ToList();
            if (methods.Count != 1)
                throw new InvalidOperationException(
                    $"Cannot identify power move callback {snapshot.PerformDeclaringType}.{snapshot.PerformMethodName}");
            callback = Delegate.CreateDelegate(parameters[1].ParameterType, powers[0], methods[0],
                throwOnBindFailure: false);
        }
        if (callback == null && useCapturedMoveCallbacks)
            callback = snapshot.OnPerformRef;
        callback ??= string.Equals(snapshot.StateId, "STUNNED", StringComparison.Ordinal)
            ? new Func<IReadOnlyList<Creature>, Task>(_ => Task.CompletedTask)
            : null;
        if (callback == null || !parameters[1].ParameterType.IsInstanceOfType(callback))
            throw new InvalidOperationException(
                $"Cannot rebuild move state {snapshot.StateId}: perform callback unavailable");

        List<object> intents;
        if (AnyMember(staticTemplate, "Intents") is System.Collections.IEnumerable templateIntents)
        {
            intents = templateIntents.Cast<object?>().Where(intent => intent != null).Cast<object>().ToList();
            if (snapshot.IntentRefs == null)
                snapshot.IntentRefs = intents;
        }
        else if (useCapturedMoveCallbacks && snapshot.IntentRefs != null)
        {
            intents = snapshot.IntentRefs;
        }
        else
        {
            intents = new List<object>();
            foreach (var typeName in snapshot.IntentTypeNames)
            {
                var intentType = typeof(CombatManager).Assembly.GetType(typeName, throwOnError: false)
                    ?? throw new InvalidOperationException(
                        $"Cannot rebuild move state {snapshot.StateId}: unknown intent type {typeName}");
                intents.Add(Activator.CreateInstance(intentType, nonPublic: true)
                    ?? throw new InvalidOperationException(
                        $"Cannot rebuild move state {snapshot.StateId}: failed to construct {typeName}"));
            }
        }

        var intentElementType = parameters[2].ParameterType.GetElementType()
            ?? throw new InvalidOperationException("MoveState intent array element type unavailable");
        var intentArray = Array.CreateInstance(intentElementType, intents.Count);
        for (var i = 0; i < intents.Count; i++)
        {
            if (!intentElementType.IsInstanceOfType(intents[i]))
                throw new InvalidOperationException(
                    $"Cannot rebuild move state {snapshot.StateId}: incompatible intent {intents[i].GetType().FullName}");
            intentArray.SetValue(intents[i], i);
        }

        var rebuilt = ctor.Invoke(new[] { snapshot.StateId, callback, intentArray });
        SetPropertyOrField(rebuilt, "FollowUpStateId", "<FollowUpStateId>k__BackingField", snapshot.FollowUpStateId);
        SetPropertyOrField(
            rebuilt,
            "MustPerformOnceBeforeTransitioning",
            "<MustPerformOnceBeforeTransitioning>k__BackingField",
            snapshot.MustPerformOnceBeforeTransitioning);
        SetField(rebuilt, "_performedAtLeastOnce", snapshot.PerformedAtLeastOnce);
        return rebuilt;
    }

    private static void SetPropertyOrField(object target, string propertyName, string fieldName, object? value)
    {
        for (var type = target.GetType(); type != null; type = type.BaseType)
        {
            var property = type.GetProperty(
                propertyName,
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (property?.SetMethod != null)
            {
                property.SetValue(target, value);
                return;
            }
        }
        SetField(target, fieldName, value);
    }

    private sealed class ReferenceEqualityComparer : IEqualityComparer<object>
    {
        public static readonly ReferenceEqualityComparer Instance = new();

        public new bool Equals(object? x, object? y) => ReferenceEquals(x, y);

        public int GetHashCode(object obj) => RuntimeHelpers.GetHashCode(obj);
    }

    private sealed class ReferenceEqualityComparerT<T> : IEqualityComparer<T> where T : class
    {
        public static readonly ReferenceEqualityComparerT<T> Instance = new();

        public bool Equals(T? x, T? y) => ReferenceEquals(x, y);

        public int GetHashCode(T obj) => RuntimeHelpers.GetHashCode(obj);
    }

    private static object? AnyMember(object? obj, string name)
    {
        if (obj == null) return null;
        for (var t = obj.GetType(); t != null; t = t.BaseType)
        {
            var p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (p != null) return p.GetValue(obj);
            var f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (f != null) return f.GetValue(obj);
            var backing = t.GetField($"<{name}>k__BackingField", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (backing != null) return backing.GetValue(obj);
        }
        return null;
    }

    /// <summary>Common context added to every decision point.</summary>
    private Dictionary<string, object?> RunContext()
    {
        if (_runState == null) return new();
        var ctx = new Dictionary<string, object?>
        {
            ["act"] = _runState.CurrentActIndex + 1,
            ["act_name"] = _loc.Act(_runState.Act?.Id.Entry ?? "OVERGROWTH"),
            ["floor"] = _runState.ActFloor,
            ["room_type"] = _runState.CurrentRoom?.RoomType.ToString(),
        };
        if (_autoResolvedRelicPicks > 0)
        {
            ctx["auto_resolved_relic_picks"] = _autoResolvedRelicPicks;
            ctx["last_auto_resolved_relic_pick"] = _lastAutoResolvedRelicPick;
        }

        // Boss encounter info — use BossEncounter?.Id?.Entry
        try
        {
            var bossIdEntry = _runState.Act?.BossEncounter?.Id?.Entry;
            if (!string.IsNullOrEmpty(bossIdEntry))
            {
                var monsterKey = bossIdEntry.EndsWith("_BOSS") ? bossIdEntry[..^5] : bossIdEntry;
                // Handle special mappings
                if (monsterKey == "THE_KIN") monsterKey = "KIN_PRIEST";
                ctx["boss"] = new Dictionary<string, object?>
                {
                    ["id"] = bossIdEntry,
                    ["name"] = _loc.Monster(monsterKey),
                };
            }
        }
        catch { }

        return ctx;
    }

    private static void EnsureModelDbInitialized()
    {
        if (_modelDbInitialized) return;
        _modelDbInitialized = true;

        TestMode.IsOn = true;

        // The headless runtime intentionally loads no gameplay mods. Current game
        // builds ask ReflectionHelper for mod-defined badge models while creating
        // a run, so make the empty mod type set explicit before ModManager exists.
        var modManagerState = typeof(MegaCrit.Sts2.Core.Modding.ModManager).GetField(
            "<State>k__BackingField", BindingFlags.Static | BindingFlags.NonPublic);
        if (modManagerState != null)
            modManagerState.SetValue(null, Enum.Parse(modManagerState.FieldType, "Skipped"));
        typeof(ReflectionHelper).GetField("_modTypes",
                BindingFlags.Static | BindingFlags.NonPublic)
            ?.SetValue(null, Array.Empty<Type>());

        // Install inline sync context on main thread
        SynchronizationContext.SetSynchronizationContext(_syncCtx);

        // Initialize PlatformServices before anything touches PlatformUtil
        try
        {
            // Try to access PlatformUtil to trigger its static init
            // If it fails, it won't be available but most code checks SteamInitializer.Initialized
            var _ = MegaCrit.Sts2.Core.Platform.PlatformUtil.PrimaryPlatform;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] PlatformUtil init: {ex.Message}");
        }

        // Initialize SaveManager with a dummy profile for save/load support
        try { SaveManager.Instance.InitProfileId(0); }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] SaveManager.InitProfileId: {ex.Message}"); }

        // Initialize progress data for epoch/timeline tracking
        try { SaveManager.Instance.InitProgressData(); }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] InitProgressData: {ex.Message}"); }

        // Initialize prefs data with defaults (FastMode = Normal). Without this,
        // SaveManager.Instance.PrefsSave (the PrefsSaveManager.Prefs auto-property)
        // is NULL, and any code that dereferences it NREs. The decompiled dll has
        // an unguarded `SaveManager.Instance.PrefsSave.FastMode` read inside
        // TalkCmd.Play (the speech-bubble timing calc), which is reached from
        // KinPriest.RitualMove's first-cast speech line (`if (!SpeechUsed)`),
        // i.e. on the first RITUAL_MOVE of every THE_KIN_BOSS fight (turn 4 with
        // no plays). The NRE aborts the enemy turn mid-resolution and strands
        // combat on the Enemy side -> nuclear fallback -> synthetic game_over
        // ("dies" at ~30 HP vs a 7-dmg follower). This is the SAME synthetic-death
        // CLASS as the Vantom DoHitStop NRE (PatchNGameVfx), but a different null
        // root: an uninitialized save singleton rather than null NGame.Instance.
        // InitPrefsDataForTest just assigns `Prefs = new PrefsSave()` (no file I/O,
        // FastMode defaults to Normal), so TalkCmd.Play computes a real duration
        // and the gameplay logic (RitualMove's StrengthPower apply) runs to
        // completion. Faithful: prefs only gate cosmetic/UI timing, never rules.
        try { SaveManager.Instance.InitPrefsDataForTest(); }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] InitPrefsDataForTest: {ex.Message}"); }

        // Install the Task.Yield patch but keep SuppressYield=false by default.
        // SuppressYield is toggled to true only during EndTurn to prevent boss fight deadlocks.
        PatchTaskYield();

        // Optional await-boundary diagnostics for enemy-turn stalls. The patches
        // are inert unless STS2_TRACE_ENEMY_TURN=1 is present in the worker.
        PatchEnemyTurnTaskTrace();

        // Patch visual delays to no-op in headless mode. Cmd.CustomScaledWait is used
        // by CreatureCmd.TriggerAnim and can otherwise strand combat between turns.
        PatchCmdWait();

        // Neutralize NGame's cosmetic screen-shake / hit-stop VFX and install a
        // non-null NGame.Instance stub. Vantom's Dismember move calls
        // `NGame.Instance.DoHitStop(...)` WITHOUT the null-conditional that every
        // other Godot call in that method uses (the dll's own latent bug). With a
        // null NGame.Instance the callvirt throws NRE mid enemy-turn AFTER the
        // damage lands but BEFORE the turn cycle returns to the play phase, which
        // strands combat on the Enemy side and the harness then reports a phantom
        // game_over (player "dies" at full-ish HP). See PatchNGameVfx.
        PatchNGameVfx();
        PatchHeadlessAudioManager();
        PatchKaiserCrabBackground();
        PatchSoulNexusPresentation();
        PatchEventAudio();
        PatchTrialPresentation();
        PatchCrystalSphereScreen();

        // Debug-only: trace Creature.LoseHpInternal to see the actual damage value
        // reaching a creature (distinguishes card-side damage inflation from
        // enemy-side amplification). Gated by DamageTrace.Enabled (off by default).
        PatchDamageTrace();

        // Initialize localization system (needed for events, cards, etc.)
        InitLocManager();

        var subtypes = MegaCrit.Sts2.Core.Models.AbstractModelSubtypes.All;
        int registered = 0, failed = 0;
        for (int i = 0; i < subtypes.Count; i++)
        {
            try
            {
                ModelDb.Inject(subtypes[i]);
                registered++;
            }
            catch (Exception ex)
            {
                failed++;
                // Only log first few failures to reduce noise
                if (failed <= 5)
                    Console.Error.WriteLine($"[WARN] Failed to register {subtypes[i].Name}: {ex.GetType().Name}: {ex.Message}");
            }
        }
        Console.Error.WriteLine($"[INFO] ModelDb: {registered} registered, {failed} failed out of {subtypes.Count}");

        // Initialize net ID serialization cache (needed for combat actions)
        try
        {
            ModelIdSerializationCache.Init();
            Console.Error.WriteLine("[INFO] ModelIdSerializationCache initialized");
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] ModelIdSerializationCache.Init: {ex.Message}");
        }
    }

    private static bool TryResolveUnlockState(
        string unlockMode,
        string? progressJson,
        out UnlockState unlockState,
        out Dictionary<string, object?> summary,
        out string error)
    {
        unlockState = UnlockState.none;
        summary = new Dictionary<string, object?>();
        error = "";

        if (string.Equals(unlockMode, "all", StringComparison.OrdinalIgnoreCase))
        {
            unlockState = UnlockState.all;
        }
        else if (string.Equals(unlockMode, "profile", StringComparison.OrdinalIgnoreCase))
        {
            if (string.IsNullOrWhiteSpace(progressJson))
            {
                error = "unlock_mode 'profile' requires progress_path or progress_json";
                return false;
            }

            var readResult = SaveManager.FromJson<SerializableProgress>(progressJson);
            if (!readResult.Success || readResult.SaveData == null)
            {
                error = $"Failed to parse progress file: {readResult.Status} {readResult.ErrorMessage}";
                return false;
            }

            var context = new MegaCrit.Sts2.Core.Saves.Validation.DeserializationContext();
            var progress = ProgressState.FromSerializable(readResult.SaveData, context);
            if (context.HasFatal)
            {
                error = $"Progress validation failed with {context.FatalCount} fatal error(s)";
                return false;
            }
            unlockState = new UnlockState(progress);
        }
        else
        {
            error = $"Unknown unlock_mode '{unlockMode}'; expected 'profile' or 'all'";
            return false;
        }

        var serializable = unlockState.ToSerializable();
        var epochs = serializable.UnlockedEpochs.OrderBy(id => id, StringComparer.Ordinal).ToList();
        var encounters = serializable.EncountersSeen
            .Select(id => id.ToString())
            .OrderBy(id => id, StringComparer.Ordinal)
            .ToList();
        var canonical = string.Join("\n", new[]
        {
            unlockMode.ToLowerInvariant(),
            serializable.NumberOfRuns.ToString(System.Globalization.CultureInfo.InvariantCulture),
            string.Join(",", epochs),
            string.Join(",", encounters),
        });
        var digest = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(canonical))).ToLowerInvariant();
        summary = new Dictionary<string, object?>
        {
            ["mode"] = unlockMode.ToLowerInvariant(),
            ["number_of_runs"] = serializable.NumberOfRuns,
            ["unlocked_epoch_count"] = epochs.Count,
            ["encounters_seen_count"] = encounters.Count,
            ["digest_sha256"] = digest,
        };
        return true;
    }

    private Player? CreatePlayer(string characterName, UnlockState unlockState)
    {
        return characterName.ToLowerInvariant() switch
        {
            "ironclad" => Player.CreateForNewRun<Ironclad>(unlockState, 1uL),
            "silent" => Player.CreateForNewRun<Silent>(unlockState, 1uL),
            "defect" => Player.CreateForNewRun<Defect>(unlockState, 1uL),
            "regent" => Player.CreateForNewRun<Regent>(unlockState, 1uL),
            "necrobinder" => Player.CreateForNewRun<Necrobinder>(unlockState, 1uL),
            _ => null
        };
    }

    private static void PatchCmdWait()
    {
        try
        {
            var harmony = new Harmony("sts2headless.cmdwait");
            // Find Cmd.Wait(float) — it's in MegaCrit.Sts2.Core.Commands namespace
            // Find Cmd type via CardPileCmd's assembly (both are in same namespace)
            var cmdPileType = typeof(MegaCrit.Sts2.Core.Commands.CardPileCmd);
            var cmdAsm = cmdPileType.Assembly;
            Type? cmdType = cmdAsm.GetType("MegaCrit.Sts2.Core.Commands.Cmd");
            // If not found by exact name, search by namespace + "Wait" method
            if (cmdType == null)
            {
                foreach (var t in cmdAsm.GetTypes())
                {
                    if (t.Namespace == "MegaCrit.Sts2.Core.Commands")
                    {
                        var waitM = t.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.DeclaredOnly)
                            .Where(m => m.Name is "Wait" or "CustomScaledWait").ToList();
                        if (waitM.Count > 0)
                        {
                            cmdType = t;
                            Console.Error.WriteLine($"[INFO] Found Wait() in {t.FullName}");
                            break;
                        }
                    }
                }
            }
            if (cmdType != null)
            {
                var waitMethod = cmdType.GetMethod("Wait",
                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static,
                    null, new[] { typeof(float) }, null);
                if (waitMethod != null)
                {
                    var prefix = typeof(YieldPatches).GetMethod(nameof(YieldPatches.CmdWaitPrefix),
                        System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                    if (prefix != null)
                    {
                        harmony.Patch(waitMethod, new HarmonyMethod(prefix));
                        Console.Error.WriteLine("[INFO] Patched Cmd.Wait() to no-op (prevents boss fight deadlocks)");
                    }
                }
                else
                {
                    // Try to find any Wait method
                    var methods = cmdType.GetMethods(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static)
                        .Where(m => m.Name is "Wait" or "CustomScaledWait").ToList();
                    foreach (var m in methods)
                    {
                        Console.Error.WriteLine($"[INFO] Found Cmd.Wait({string.Join(",", m.GetParameters().Select(p => p.ParameterType.Name))})");
                        var prefix = typeof(YieldPatches).GetMethod(nameof(YieldPatches.CmdWaitPrefix),
                            System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                        if (prefix != null)
                        {
                            harmony.Patch(m, new HarmonyMethod(prefix));
                            Console.Error.WriteLine($"[INFO] Patched Cmd.Wait variant");
                        }
                    }
                }
            }
            else
            {
                Console.Error.WriteLine("[WARN] Could not find MegaCrit.Sts2.Core.Commands.Cmd type");
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Failed to patch Cmd.Wait: {ex.Message}");
        }
    }

    // Install a non-null NGame.Instance stub and no-op NGame's cosmetic VFX
    // methods (screen shake / hit-stop). These are purely visual and have zero
    // gameplay effect, but the dll calls some of them on a NULL-unguarded
    // receiver (Vantom.DismemberMove: `NGame.Instance.DoHitStop(...)`), so a null
    // Instance throws an NRE that aborts the enemy turn before the play phase is
    // restored. Two parts are both required:
    //   (1) Instance must be non-null, because `callvirt` null-checks the
    //       receiver BEFORE dispatch — patching the method body alone can't stop
    //       the NRE on `NGame.Instance.DoHitStop`.
    //   (2) the VFX methods must be no-op'd, because once Instance is a bare
    //       uninitialized stub its internal Godot fields (_screenShake, HitStop)
    //       are null, and combat fires `NGame.Instance?.ScreenShake(...)` on
    //       essentially every attack — those must not dereference null fields.
    // This mirrors the existing Cmd.Wait / PreviewCardPileAdd neutralization: the
    // cosmetic call is skipped while the surrounding gameplay logic (here, the
    // Dismember Wound-add that follows DoHitStop) runs to completion.
    private static bool CaptureCrystalSphereScreen(CrystalSphereMinigame grid,
        ref NCrystalSphereScreen __result)
    {
        _activeCrystalSphere = grid;
        __result = null!;
        return false;
    }

    private static void PatchCrystalSphereScreen()
    {
        var show = typeof(NCrystalSphereScreen).GetMethod(nameof(NCrystalSphereScreen.ShowScreen),
            BindingFlags.Public | BindingFlags.Static)
            ?? throw new MissingMethodException("NCrystalSphereScreen.ShowScreen");
        var prefix = typeof(RunSimulator).GetMethod(nameof(CaptureCrystalSphereScreen),
            BindingFlags.NonPublic | BindingFlags.Static)
            ?? throw new MissingMethodException(nameof(CaptureCrystalSphereScreen));
        new Harmony("sts2headless.crystal-sphere-screen").Patch(show,
            prefix: new HarmonyMethod(prefix));
    }

    private static void PatchEventAudio()
    {
        var audio = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Audio.Debug.NDebugAudioManager")
            ?? throw new InvalidOperationException("Missing native debug audio type");
        var harmony = new Harmony("sts2headless.eventaudio");
        harmony.Patch(audio.GetProperty("Instance")!.GetMethod!,
            prefix: new HarmonyMethod(typeof(RunSimulator).GetMethod(nameof(EventAudioInstance), BindingFlags.Static | BindingFlags.NonPublic)!));
        harmony.Patch(audio.GetMethod("Play")!,
            prefix: new HarmonyMethod(typeof(RunSimulator).GetMethod(nameof(EventAudioPlay), BindingFlags.Static | BindingFlags.NonPublic)!));
        foreach (var name in new[] { "Stop", "StopAll" })
            harmony.Patch(audio.GetMethod(name)!,
                prefix: new HarmonyMethod(typeof(RunSimulator).GetMethod(nameof(EventAudioStop), BindingFlags.Static | BindingFlags.NonPublic)!));
        _eventAudioStub = System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(audio);
    }

    private static bool TrialHasVisibleRoom(Player? player) =>
        LocalContext.IsMe(player)
        && MegaCrit.Sts2.Core.Nodes.Rooms.NEventRoom.Instance?.Layout != null;

    private static IEnumerable<CodeInstruction> GuardTrialPresentation(
        IEnumerable<CodeInstruction> instructions, MethodBase original)
    {
        var localCheck = typeof(LocalContext).GetMethod(nameof(LocalContext.IsMe),
            new[] { typeof(Player) })
            ?? throw new MissingMethodException("LocalContext.IsMe(Player)");
        var guarded = typeof(RunSimulator).GetMethod(nameof(TrialHasVisibleRoom),
            BindingFlags.NonPublic | BindingFlags.Static)
            ?? throw new MissingMethodException(nameof(TrialHasVisibleRoom));
        var count = 0;
        foreach (var instruction in instructions)
        {
            if ((instruction.opcode == OpCodes.Call || instruction.opcode == OpCodes.Callvirt)
                && instruction.operand is MethodInfo method && method.Equals(localCheck))
            {
                instruction.operand = guarded;
                count++;
            }
            yield return instruction;
        }
        var expected = original.Name == "Accept" ? 2 : 1;
        if (count != expected)
            throw new InvalidOperationException(
                $"Trial presentation guard expected {expected} local UI checks in {original.Name}, found {count}");
    }

    private static void PatchTrialPresentation()
    {
        var trial = typeof(MegaCrit.Sts2.Core.Models.Events.Trial);
        var transpiler = typeof(RunSimulator).GetMethod(nameof(GuardTrialPresentation),
            BindingFlags.NonPublic | BindingFlags.Static)
            ?? throw new MissingMethodException(nameof(GuardTrialPresentation));
        var harmony = new Harmony("sts2headless.trial-presentation");
        foreach (var name in new[] { "Accept", "AddVfxAnchoredToPortrait" })
        {
            var method = trial.GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance)
                ?? throw new MissingMethodException($"Trial.{name}");
            harmony.Patch(method, transpiler: new HarmonyMethod(transpiler));
        }
    }

    private static object? _eventAudioStub;
    private static bool EventAudioInstance(ref object? __result) { __result = _eventAudioStub; return false; }
    private static bool EventAudioPlay(ref int __result) { __result = 0; return false; }
    private static bool EventAudioStop() => false;

    private static object? _headlessAudioManager;

    private static void PatchHeadlessAudioManager()
    {
        // Some native death hooks call NAudioManager.Instance without a null
        // check.  Let those hooks and their gameplay continuations run while
        // replacing only presentation calls in this scene-less process.
        var audioType = typeof(MegaCrit.Sts2.Core.Nodes.Audio.NAudioManager);
        _headlessAudioManager = RuntimeHelpers.GetUninitializedObject(audioType);
        var harmony = new Harmony("sts2headless.audio-manager");
        var instancePrefix = typeof(RunSimulator).GetMethod(nameof(HeadlessAudioInstance),
            BindingFlags.Static | BindingFlags.NonPublic)!;
        var noOpPrefix = typeof(RunSimulator).GetMethod(nameof(HeadlessAudioNoOp),
            BindingFlags.Static | BindingFlags.NonPublic)!;
        harmony.Patch(audioType.GetProperty("Instance", BindingFlags.Static | BindingFlags.Public)!.GetMethod!,
            prefix: new HarmonyMethod(instancePrefix));
        var presentationMethods = new HashSet<string> {
            "PlayLoop", "StopLoop", "SetParam", "StopAllLoops", "PlayOneShot",
            "PlayMusic", "UpdateMusicParameter", "StopMusic", "SetMasterVol",
            "SetSfxVol", "SetAmbienceVol", "SetBgmVol",
        };
        foreach (var method in audioType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.DeclaredOnly)
                     .Where(method => method.ReturnType == typeof(void)
                                      && presentationMethods.Contains(method.Name)))
            harmony.Patch(method, prefix: new HarmonyMethod(noOpPrefix));
    }

    private static bool HeadlessAudioInstance(ref MegaCrit.Sts2.Core.Nodes.Audio.NAudioManager __result)
    {
        __result = (MegaCrit.Sts2.Core.Nodes.Audio.NAudioManager)_headlessAudioManager!;
        return false;
    }

    private static bool HeadlessAudioNoOp() => false;

    private static void PatchSoulNexusPresentation()
    {
        // SoulNexus.AfterDeath only removes its death callback and animates the
        // room node. The room node does not exist in a headless simulation.
        // Keep the callback removal so a restored creature cannot retain it.
        var monsterType = typeof(PlayCardAction).Assembly.GetType(
            "MegaCrit.Sts2.Core.Models.Monsters.SoulNexus")!;
        var afterDeath = monsterType.GetMethod("AfterDeath",
            BindingFlags.Instance | BindingFlags.NonPublic)!;
        var prefix = typeof(RunSimulator).GetMethod(nameof(SoulNexusAfterDeathPrefix),
            BindingFlags.Static | BindingFlags.NonPublic)!;
        new Harmony("sts2headless.soulnexus.presentation").Patch(afterDeath,
            prefix: new HarmonyMethod(prefix));
    }

    private static bool SoulNexusAfterDeathPrefix(object __instance)
    {
        var creature = ((MonsterModel)__instance).Creature;
        var method = __instance.GetType().GetMethod("AfterDeath",
            BindingFlags.Instance | BindingFlags.NonPublic)!;
        creature.Died -= (Action<Creature>)Delegate.CreateDelegate(
            typeof(Action<Creature>), __instance, method);
        return false;
    }

    private static void PatchKaiserCrabBackground()
    {
        // The native monster hooks apply BackAttack/CrabRage before accessing
        // this scene-only background. Preserve the hooks and all game effects;
        // supply just the absent visual node and no-op its animation methods.
        var harmony = new Harmony("sts2headless.kaiserbackground");
        var backgroundType = typeof(MegaCrit.Sts2.Core.Nodes.Vfx.Backgrounds.NKaiserCrabBossBackground);
        var getterPrefix = typeof(RunSimulator).GetMethod(nameof(KaiserBackgroundPrefix),
            BindingFlags.Static | BindingFlags.NonPublic)!;
        foreach (var name in new[] { "Crusher", "Rocket" })
        {
            var type = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Models.Monsters." + name)!;
            var getter = type.GetProperty("Background", BindingFlags.Instance | BindingFlags.NonPublic)!.GetMethod!;
            harmony.Patch(getter, prefix: new HarmonyMethod(getterPrefix));
        }
        foreach (var name in new[] { "PlayAttackAnim", "PlayHurtAnim", "PlayArmDeathAnim",
                     "PlayRightSideChargeUpAnim", "PlayRightSideHeavy", "PlayRightRecharge", "PlayBodyDeathAnim" })
        {
            var method = backgroundType.GetMethod(name, BindingFlags.Instance | BindingFlags.Public)!;
            var prefix = typeof(YieldPatches).GetMethod(method.ReturnType == typeof(Task)
                ? nameof(YieldPatches.CmdWaitPrefix) : nameof(YieldPatches.VoidNoOpPrefix))!;
            harmony.Patch(method, prefix: new HarmonyMethod(prefix));
        }
    }

    private static bool KaiserBackgroundPrefix(object __instance,
        ref MegaCrit.Sts2.Core.Nodes.Vfx.Backgrounds.NKaiserCrabBossBackground __result)
    {
        var field = __instance.GetType().GetField("_background", BindingFlags.Instance | BindingFlags.NonPublic)!;
        __result = (MegaCrit.Sts2.Core.Nodes.Vfx.Backgrounds.NKaiserCrabBossBackground?)field.GetValue(__instance)
            ?? new MegaCrit.Sts2.Core.Nodes.Vfx.Backgrounds.NKaiserCrabBossBackground();
        field.SetValue(__instance, __result);
        return false;
    }

    private static void PatchNGameVfx()
    {
        try
        {
            var harmony = new Harmony("sts2headless.ngamevfx");
            var ngameType = typeof(PlayCardAction).Assembly
                .GetType("MegaCrit.Sts2.Core.Nodes.NGame");
            if (ngameType == null)
            {
                Console.Error.WriteLine("[WARN] PatchNGameVfx: NGame type not found");
                return;
            }

            // No-op the cosmetic VFX methods reachable from combat. Each is a void
            // instance method whose only effect is visual; skipping the original
            // (prefix returns false) is faithful in a headless, no-scene run.
            var voidNoOp = typeof(YieldPatches).GetMethod(
                nameof(YieldPatches.VoidNoOpPrefix),
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            foreach (var name in new[]
            {
                "DoHitStop", "ScreenShake", "ScreenRumble", "ScreenShakeTrauma",
                "SetScreenShakeTarget", "ClearScreenShakeTarget", "SetScreenshakeMultiplier",
            })
            {
                foreach (var m in ngameType.GetMethods(
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public)
                    .Where(m => m.Name == name && m.ReturnType == typeof(void)))
                {
                    try
                    {
                        harmony.Patch(m, new HarmonyMethod(voidNoOp));
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine($"[WARN] PatchNGameVfx {name}: {ex.Message}");
                    }
                }
            }

            // Install a non-null Instance. Use an uninitialized object so we don't
            // run NGame's Godot _Ready/ctor (no scene tree). Its VFX methods are
            // now no-ops, so the null internal fields are never touched.
            try
            {
                var instProp = ngameType.GetProperty("Instance",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                if (instProp?.GetValue(null) == null)
                {
                    var stub = System.Runtime.CompilerServices.RuntimeHelpers
                        .GetUninitializedObject(ngameType);

                    // Give the stub a non-null RootSceneContainer whose CurrentScene
                    // is null. NGame's computed getters CurrentRunNode/MainMenu/
                    // LogoAnimation are `RootSceneContainer.CurrentScene as T`, and
                    // NRun.Instance is `NGame.Instance?.CurrentRunNode`. With a null
                    // Instance these all short-circuited to null; a bare stub would
                    // instead NRE dereferencing the null RootSceneContainer (observed
                    // in RunManager.ExitCurrentRooms via NRun.Instance during load).
                    // An uninitialized NSceneContainer has _currentScene == null, so
                    // its CurrentScene getter returns null and those getters once
                    // again resolve to null — preserving the prior behavior exactly.
                    try
                    {
                        var containerType = typeof(PlayCardAction).Assembly
                            .GetType("MegaCrit.Sts2.Core.Nodes.NSceneContainer");
                        if (containerType != null)
                        {
                            var container = System.Runtime.CompilerServices.RuntimeHelpers
                                .GetUninitializedObject(containerType);
                            SetField(stub, "<RootSceneContainer>k__BackingField", container);
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine($"[WARN] PatchNGameVfx RootSceneContainer: {ex.Message}");
                    }

                    // Instance is a static auto-property; set its static backing field.
                    var backing = ngameType.GetField("<Instance>k__BackingField",
                        System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic
                        | System.Reflection.BindingFlags.Public);
                    if (backing != null)
                        backing.SetValue(null, stub);
                    else
                        instProp?.GetSetMethod(true)?.Invoke(null, new[] { stub });
                }
                Console.Error.WriteLine("[INFO] Patched NGame VFX to no-op + installed NGame.Instance stub");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[WARN] PatchNGameVfx install Instance: {ex.Message}");
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Failed to patch NGame VFX: {ex.Message}");
        }
    }

    internal static class DamageTrace
    {
        public static bool Enabled = false;
        public static readonly List<string> Log = new();
        public static bool LoseHpPrefix(object __instance, decimal amount)
        {
            if (Enabled)
            {
                try { Log.Add($"{__instance.GetType().Name}:{amount}"); } catch { }
            }
            return true; // run original
        }
        public static void ScalingPostfix(object __result)
        {
            if (Enabled)
            {
                try { Log.Add($"scaling={__result}"); } catch { }
            }
        }
        // Generic postfix for any Modify*Damage* hook: logs declaring type + result
        // so we can see which modifier(s) fire (and how many times) per card play.
        public static void ModifyDamagePostfix(System.Reflection.MethodBase __originalMethod, object __result)
        {
            if (Enabled)
            {
                try { Log.Add($"{__originalMethod.DeclaringType?.Name}.{__originalMethod.Name}=>{__result}"); } catch { }
            }
        }
        // When SlowPower's multiplier is computed, dump its DynamicVars + the
        // dealer creature's relevant counters to locate the per-turn cards-played
        // tally that drives the +10%/card and that in_place restore fails to reset.
        public static void SlowMultPostfix(object __instance, object __result, object? dealer)
        {
            if (!Enabled) return;
            try
            {
                var sb = new System.Text.StringBuilder($"SlowMult=>{__result}|");
                var dvProp = __instance.GetType().GetProperty("DynamicVars", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (dvProp?.GetValue(__instance) is System.Collections.IEnumerable dvs)
                {
                    // DynamicVars is usually a dictionary; enumerate values
                    foreach (var item in dvs)
                    {
                        var val = item;
                        var vp = item.GetType().GetProperty("Value");
                        if (vp != null) val = vp.GetValue(item);
                        var nameP = val?.GetType().GetProperty("Name");
                        var baseP = val?.GetType().GetProperty("BaseValue");
                        if (nameP != null && baseP != null)
                            try { sb.Append($"dv.{nameP.GetValue(val)}={baseP.GetValue(val)};"); } catch { }
                    }
                }
                Log.Add(sb.ToString());
            }
            catch (Exception ex) { Log.Add($"SlowMult err {ex.Message}"); }
        }
        public static void WasExhaustedPostfix(object __instance, object __result)
        {
            if (!Enabled) return;
            try
            {
                var sb = new System.Text.StringBuilder($"WasExhaustedThisTurn=>{__result}|");
                var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);
                void W(string p, object? o, int d)
                {
                    if (o == null || d > 4 || !seen.Add(o)) return;
                    var t = o.GetType();
                    var fn = t.FullName ?? "";
                    if (fn.StartsWith("System") || fn.StartsWith("Godot")) return;
                    foreach (var f in t.GetFields(BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic))
                    {
                        object? v = null; try { v = f.GetValue(o); } catch { continue; }
                        if (v == null) continue;
                        var vt = v.GetType();
                        var fl = f.Name.ToLowerInvariant();
                        if ((vt.IsPrimitive || vt.IsEnum))
                        {
                            if (fl.Contains("exhaust") || fl.Contains("turn") || fl.Contains("played") || fl.Contains("count") || fl.Contains("history"))
                                sb.Append($"{p}.{f.Name}={v};");
                        }
                        else if (v is System.Collections.ICollection col && (fl.Contains("history") || fl.Contains("exhaust") || fl.Contains("played") || fl.Contains("event")))
                        {
                            sb.Append($"{p}.{f.Name}.Count={col.Count};");
                        }
                        else if (!(v is string))
                            W($"{p}.{f.Name}", v, d + 1);
                    }
                }
                W("card", __instance, 0);
                // also walk the live combat state graph for an exhaust/turn tally
                try
                {
                    var cmType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Combat.CombatManager");
                    var inst = cmType?.GetProperty("Instance", BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static)?.GetValue(null);
                    var cs = inst?.GetType().GetMethod("DebugOnlyGetState")?.Invoke(inst, null);
                    if (cs != null) W("cs", cs, 0);
                }
                catch { }
                Log.Add(sb.ToString());
            }
            catch (Exception ex) { Log.Add($"WasExh err {ex.Message}"); }
        }
        public static void AfterCardPlayedPostfix(object __instance)
        {
            if (Enabled)
            {
                // dump SlowPower's internal counter fields after each card-played hook
                try
                {
                    var sb = new System.Text.StringBuilder("SlowPower.AfterCardPlayed{");
                    foreach (var f in __instance.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                    {
                        var v = f.GetValue(__instance);
                        if (v != null && (v.GetType().IsPrimitive || v.GetType().IsEnum))
                            sb.Append($"{f.Name}={v};");
                    }
                    // also walk into reference-type fields one level for a counter
                    foreach (var f in __instance.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                    {
                        object? v = null; try { v = f.GetValue(__instance); } catch { }
                        if (v == null || v.GetType().IsPrimitive || v is string || v.GetType().IsEnum) continue;
                        var fn2 = v.GetType().FullName ?? "";
                        if (fn2.StartsWith("System") || fn2.StartsWith("Godot")) continue;
                        foreach (var f2 in v.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                        {
                            object? v2 = null; try { v2 = f2.GetValue(v); } catch { }
                            if (v2 != null && (v2.GetType().IsPrimitive || v2.GetType().IsEnum))
                                sb.Append($"{f.Name}.{f2.Name}={v2};");
                        }
                    }
                    sb.Append("}");
                    Log.Add(sb.ToString());
                }
                catch { }
            }
        }
    }

    private static void PatchDamageTrace()
    {
        try
        {
            var harmony = new Harmony("sts2headless.damagetrace");
            var creatureType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Entities.Creatures.Creature");
            var m = creatureType?.GetMethod("LoseHpInternal", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            var prefix = typeof(DamageTrace).GetMethod(nameof(DamageTrace.LoseHpPrefix), BindingFlags.Static | BindingFlags.Public);
            if (m != null && prefix != null)
            {
                harmony.Patch(m, new HarmonyMethod(prefix));
                Console.Error.WriteLine("[INFO] Patched Creature.LoseHpInternal for damage trace");
            }
            // Trace the multiplayer scaling factor (suspect: stacks per restore).
            var scalingType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Models.Singleton.MultiplayerScalingModel");
            var sm = scalingType?.GetMethod("GetMultiplayerScaling", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            var postfix = typeof(DamageTrace).GetMethod(nameof(DamageTrace.ScalingPostfix), BindingFlags.Static | BindingFlags.Public);
            if (sm != null && postfix != null)
            {
                harmony.Patch(sm, postfix: new HarmonyMethod(postfix));
                Console.Error.WriteLine("[INFO] Patched MultiplayerScalingModel.GetMultiplayerScaling for trace");
            }
            // Patch every Modify*Damage* method across the game assembly so we can
            // see which damage modifier fires (and how often) during a card play.
            var modPostfix = typeof(DamageTrace).GetMethod(nameof(DamageTrace.ModifyDamagePostfix), BindingFlags.Static | BindingFlags.Public);
            if (modPostfix != null)
            {
                Type[] types;
                try { types = typeof(PlayCardAction).Assembly.GetTypes(); }
                catch (ReflectionTypeLoadException rtle) { types = rtle.Types.Where(t => t != null).ToArray()!; }
                int patched = 0;
                foreach (var t in types)
                {
                    if (t == null || (t.Namespace ?? "").StartsWith("MegaCrit.Sts2") == false) continue;
                    foreach (var mi in t.GetMethods(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                    {
                        if (mi.IsAbstract || mi.ContainsGenericParameters) continue;
                        if (mi.Name.IndexOf("ModifyDamage", StringComparison.Ordinal) < 0) continue;
                        if (mi.ReturnType != typeof(decimal) && mi.ReturnType != typeof(int)) continue;
                        try { harmony.Patch(mi, postfix: new HarmonyMethod(modPostfix)); patched++; } catch { }
                    }
                }
                Console.Error.WriteLine($"[INFO] Patched {patched} Modify*Damage* methods for trace");
            }
            // Trace SlowPower.AfterCardPlayed to see its internal counter per play.
            var slowType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Models.Powers.SlowPower");
            var acp = slowType?.GetMethod("AfterCardPlayed", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            var acpPost = typeof(DamageTrace).GetMethod(nameof(DamageTrace.AfterCardPlayedPostfix), BindingFlags.Static | BindingFlags.Public);
            if (acp != null && acpPost != null)
            {
                try { harmony.Patch(acp, postfix: new HarmonyMethod(acpPost)); Console.Error.WriteLine("[INFO] Patched SlowPower.AfterCardPlayed"); } catch { }
            }
            var smm = slowType?.GetMethod("ModifyDamageMultiplicative", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            var smmPost = typeof(DamageTrace).GetMethod(nameof(DamageTrace.SlowMultPostfix), BindingFlags.Static | BindingFlags.Public);
            if (smm != null && smmPost != null)
            {
                try { harmony.Patch(smm, postfix: new HarmonyMethod(smmPost)); Console.Error.WriteLine("[INFO] Patched SlowPower.ModifyDamageMultiplicative"); } catch { }
            }
            // Trace FORGOTTEN_RITUAL's "was a card exhausted this turn" getter +
            // dump the receiver (card) graph to find the leaked per-turn flag.
            var frType = typeof(PlayCardAction).Assembly.GetType("MegaCrit.Sts2.Core.Models.Cards.ForgottenRitual");
            var frGetter = frType?.GetMethod("get_WasCardExhaustedThisTurn", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            var frPost = typeof(DamageTrace).GetMethod(nameof(DamageTrace.WasExhaustedPostfix), BindingFlags.Static | BindingFlags.Public);
            if (frGetter != null && frPost != null)
            {
                try { harmony.Patch(frGetter, postfix: new HarmonyMethod(frPost)); Console.Error.WriteLine("[INFO] Patched FR.get_WasCardExhaustedThisTurn"); } catch { }
            }
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] PatchDamageTrace: {ex.Message}"); }
    }

    private static void PatchTaskYield()
    {
        try
        {
            var harmony = new Harmony("sts2headless.yieldpatch");

            // Patch YieldAwaitable.YieldAwaiter.IsCompleted to return true
            // This makes `await Task.Yield()` execute synchronously (continuation runs inline)
            var yieldAwaiterType = typeof(System.Runtime.CompilerServices.YieldAwaitable)
                .GetNestedType("YieldAwaiter");
            if (yieldAwaiterType != null)
            {
                var isCompletedProp = yieldAwaiterType.GetProperty("IsCompleted");
                if (isCompletedProp != null)
                {
                    var getter = isCompletedProp.GetGetMethod();
                    var prefix = typeof(YieldPatches).GetMethod(nameof(YieldPatches.IsCompletedPrefix),
                        System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                    if (getter != null && prefix != null)
                    {
                        harmony.Patch(getter, new HarmonyMethod(prefix));
                        Console.Error.WriteLine("[INFO] Patched Task.Yield() to be synchronous");
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Failed to patch Task.Yield: {ex.Message}");
        }
    }

    private static void PatchEnemyTurnTaskTrace()
    {
        try
        {
            var harmony = new Harmony("sts2headless.enemyturntrace");
            var postfix = typeof(EnemyTurnTaskTrace).GetMethod(
                nameof(EnemyTurnTaskTrace.TaskPostfix),
                BindingFlags.Static | BindingFlags.Public);
            if (postfix == null)
                return;

            var names = new HashSet<string>(StringComparer.Ordinal)
            {
                "StartTurn",
                "ExecuteEnemyTurn",
                "EndEnemyTurn",
                "EndEnemyTurnInternal",
                "CheckWinCondition",
                "WaitForUnpause",
            };
            foreach (var method in typeof(CombatManager).GetMethods(
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
            {
                if (names.Contains(method.Name) && typeof(Task).IsAssignableFrom(method.ReturnType))
                    harmony.Patch(method, postfix: new HarmonyMethod(postfix));
            }

            var takeTurn = typeof(Creature).GetMethod(
                "TakeTurn",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                binder: null,
                types: Type.EmptyTypes,
                modifiers: null);
            if (takeTurn != null && typeof(Task).IsAssignableFrom(takeTurn.ReturnType))
                harmony.Patch(takeTurn, postfix: new HarmonyMethod(postfix));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Failed to patch enemy-turn task trace: {ex.Message}");
        }
    }

    /// <summary>
    /// Card selector for headless mode — picks first available card for any selection prompt.
    /// Used by cards like Headbutt, Armaments, etc. that need player to choose a card.
    /// </summary>
    /// <summary>
    /// Card selector that creates a pending selection decision point.
    /// When the game needs the player to choose cards (upgrade, remove, transform, bundle pick),
    /// this stores the options and waits for the main loop to provide the answer.
    /// </summary>
    internal class HeadlessCardSelector : MegaCrit.Sts2.Core.TestSupport.ICardSelector
    {
        // Pending card selection — set by game engine, read by main loop
        public List<CardModel>? PendingOptions { get; private set; }
        public int PendingMinSelect { get; private set; }
        public int PendingMaxSelect { get; private set; }
        public string PendingPrompt { get; private set; } = "";
        private TaskCompletionSource<IEnumerable<CardModel>>? _pendingTcs;

        public bool HasPending => _pendingTcs != null && !_pendingTcs.Task.IsCompleted;

        public Task<IEnumerable<CardModel>> GetSelectedCards(
            IEnumerable<CardModel> options, int minSelect, int maxSelect)
        {
            var optList = options.ToList();
            if (optList.Count == 0)
                return Task.FromResult<IEnumerable<CardModel>>(Array.Empty<CardModel>());

            // Forced combat picks do not need a decision boundary. Non-combat
            // selectors must stay visible so the client and shadow cannot diverge.
            if (optList.Count == 1 && minSelect >= 1 && CombatManager.Instance.IsInProgress)
                return Task.FromResult<IEnumerable<CardModel>>(optList);

            // Store pending selection and wait
            PendingOptions = optList;
            PendingMinSelect = minSelect;
            PendingMaxSelect = maxSelect;
            _pendingTcs = new TaskCompletionSource<IEnumerable<CardModel>>();

            Console.Error.WriteLine($"[SIM] Card selection pending: {optList.Count} options, select {minSelect}-{maxSelect}");

            // Return the task — the main loop will complete it
            return _pendingTcs.Task;
        }

        public void ResolvePending(IEnumerable<CardModel> selected)
        {
            _pendingTcs?.TrySetResult(selected);
            PendingOptions = null;
            _pendingTcs = null;
        }

        public void ResolvePendingByIndices(int[] indices)
        {
            if (PendingOptions == null) return;
            var selected = indices
                .Where(i => i >= 0 && i < PendingOptions.Count)
                .Select(i => PendingOptions[i])
                .ToList();
            ResolvePending(selected);
        }

        public void CancelPending()
        {
            _pendingTcs?.TrySetResult(Array.Empty<CardModel>());
            PendingOptions = null;
            _pendingTcs = null;
        }

        // Pending card reward from events (GetSelectedCardReward blocks until resolved)
        public List<MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult>? PendingRewardCards { get; private set; }
        private ManualResetEventSlim? _rewardWait;
        private int _rewardChoice = -1;

        public MegaCrit.Sts2.Core.TestSupport.CardRewardSelection GetSelectedCardReward(
            IReadOnlyList<MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult> options,
            IReadOnlyList<CardRewardAlternative> alternatives)
        {
            if (options.Count == 0) return default;

            // Store pending and block until main loop resolves
            PendingRewardCards = options.ToList();
            _rewardChoice = -1;
            _rewardWait = new ManualResetEventSlim(false);

            Console.Error.WriteLine($"[SIM] Card reward pending: {options.Count} cards (blocking)");
            _rewardWait.Wait(TimeSpan.FromSeconds(300)); // Wait up to 5 min

            var choice = _rewardChoice;
            PendingRewardCards = null;
            _rewardWait = null;

            if (choice >= 0 && choice < options.Count)
                return new MegaCrit.Sts2.Core.TestSupport.CardRewardSelection
                {
                    card = options[choice].Card,
                    alternative = null,
                };
            if (choice == -2)
            {
                var skip = alternatives.FirstOrDefault(a =>
                    string.Equals(a.OptionId?.ToString(), "Skip", StringComparison.OrdinalIgnoreCase));
                if (skip != null)
                    return new MegaCrit.Sts2.Core.TestSupport.CardRewardSelection
                    {
                        card = null,
                        alternative = skip,
                    };
            }
            return default;  // Skip
        }

        public bool HasPendingReward => PendingRewardCards != null && _rewardWait != null && !_rewardWait.IsSet;

        public void ResolveReward(int index)
        {
            _rewardChoice = index;
            _rewardWait?.Set();
        }

        public void SkipReward()
        {
            _rewardChoice = -2;
            _rewardWait?.Set();
        }
    }

    internal static class YieldPatches
    {
        // Only suppress Task.Yield() when this flag is set (during end_turn processing)
        public static volatile bool SuppressYield;

        public static bool IsCompletedPrefix(ref bool __result)
        {
            if (SuppressYield)
            {
                __result = true;
                return false;
            }
            return true; // Let normal Yield behavior run
        }

        /// <summary>Harmony prefix: make Cmd.Wait() return completed task immediately (no-op in headless).</summary>
        public static bool CmdWaitPrefix(ref Task __result)
        {
            __result = Task.CompletedTask;
            return false; // Skip original method
        }

        /// <summary>Harmony prefix: skip a void cosmetic method entirely (headless no-op).</summary>
        public static bool VoidNoOpPrefix()
        {
            return false; // Skip original method
        }
    }

    internal static class EnemyTurnTaskTrace
    {
        private sealed record ActiveTask(
            string Method,
            string Instance,
            long StartedTimestamp,
            Task Task);

        private static readonly System.Collections.Concurrent.ConcurrentDictionary<int, ActiveTask> Active = new();

        private static bool Enabled =>
            Environment.GetEnvironmentVariable("STS2_TRACE_ENEMY_TURN") == "1";

        public static void TaskPostfix(
            object? __instance,
            MethodBase __originalMethod,
            Task __result)
        {
            if (!Enabled || __result == null || __result.Status == TaskStatus.RanToCompletion)
                return;

            var taskId = __result.Id;
            Active[taskId] = new ActiveTask(
                __originalMethod.DeclaringType?.Name + "." + __originalMethod.Name,
                DescribeInstance(__instance),
                Stopwatch.GetTimestamp(),
                __result);
            _ = __result.ContinueWith(
                completed =>
                {
                    if (completed.Status == TaskStatus.RanToCompletion)
                        Active.TryRemove(taskId, out var removed);
                },
                CancellationToken.None,
                TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
        }

        public static List<Dictionary<string, object?>> Snapshot()
        {
            if (!Enabled)
                return new List<Dictionary<string, object?>>();

            return Active.Values
                .OrderBy(entry => entry.StartedTimestamp)
                .Select(entry => new Dictionary<string, object?>
                {
                    ["method"] = entry.Method,
                    ["instance"] = entry.Instance,
                    ["status"] = entry.Task.Status.ToString(),
                    ["age_ms"] = Stopwatch.GetElapsedTime(entry.StartedTimestamp).TotalMilliseconds,
                    ["exception"] = entry.Task.Exception?.GetBaseException().ToString(),
                })
                .ToList();
        }

        public static void Reset() => Active.Clear();

        private static string DescribeInstance(object? instance)
        {
            if (instance is Creature creature)
            {
                var monsterId = creature.Monster?.Id.Entry ?? creature.Name ?? creature.GetType().Name;
                var move = creature.Monster?.NextMove?.GetType().Name ?? "none";
                return $"{monsterId}:{move}:hp={creature.CurrentHp}";
            }
            return instance?.GetType().Name ?? "static";
        }
    }

    private static void InitLocManager()
    {
        // Create a LocManager instance with stub tables via reflection.
        // LocManager.Initialize() fails because PlatformUtil isn't available,
        // and Harmony can't patch some LocString methods due to JIT issues.
        // Solution: create an uninitialized LocManager, set its _tables, and
        // use Harmony only for the simple LocTable.GetRawText fallback.
        try
        {
            // Create uninitialized LocManager and set Instance
            var instanceProp = typeof(LocManager).GetProperty("Instance",
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            var instance = System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(LocManager));
            instanceProp!.SetValue(null, instance);

            // Load REAL localization data from localization_eng/ JSON files
            var tablesField = typeof(LocManager).GetField("_tables",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            var tables = new Dictionary<string, LocTable>();

            var locDir = Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", "localization_eng");
            if (Directory.Exists(locDir))
            {
                foreach (var file in Directory.GetFiles(locDir, "*.json"))
                {
                    try
                    {
                        var name = Path.GetFileNameWithoutExtension(file);
                        var data = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(
                            File.ReadAllText(file));
                        if (data != null)
                            tables[name] = new LocTable(name, data);
                    }
                    catch { }
                }
                Console.Error.WriteLine($"[INFO] Loaded {tables.Count} localization tables from {locDir}");
            }
            else
            {
                Console.Error.WriteLine($"[WARN] Localization dir not found: {locDir}");
                // Fallback: empty tables
                var tableNames = new[] {
                    "achievements","acts","afflictions","ancients","ascension",
                    "bestiary","card_keywords","card_library","card_reward_ui",
                    "card_selection","cards","characters","combat_messages",
                    "credits","enchantments","encounters","epochs","eras",
                    "events","ftues","game_over_screen","gameplay_ui",
                    "inspect_relic_screen","intents","main_menu_ui","map",
                    "merchant_room","modifiers","monsters","orbs","potion_lab",
                    "potions","powers","relic_collection","relics","rest_site_ui",
                    "run_history","settings_ui","static_hover_tips","stats_screen",
                    "timeline","vfx"
                };
                foreach (var name in tableNames)
                    tables[name] = new LocTable(name, new Dictionary<string, string>());
            }
            tablesField!.SetValue(instance, tables);

            // Set Language
            var langProp = typeof(LocManager).GetProperty("Language",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public);
            try { langProp?.SetValue(instance, "eng"); } catch { }

            // Set CultureInfo
            var cultureProp = typeof(LocManager).GetProperty("CultureInfo",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public);
            try { cultureProp?.SetValue(instance, System.Globalization.CultureInfo.InvariantCulture); } catch { }

            // Initialize _smartFormatter — the game uses `new SmartFormatter()`
            try
            {
                var sfField = typeof(LocManager).GetField("_smartFormatter",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic);
                // Dump ALL fields (instance + static)
                foreach (var f in typeof(LocManager).GetFields(System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public))
                    Console.Error.WriteLine($"[DEBUG] LocManager {(f.IsStatic?"static":"inst")} field: {f.Name} ({f.FieldType.Name})");
                Console.Error.WriteLine($"[DEBUG] sfField: {sfField?.Name ?? "null"} type: {sfField?.FieldType?.Name ?? "null"}");
                if (sfField != null)
                {
                    try
                    {
                        // List constructors to find the right one
                        var ctors = sfField.FieldType.GetConstructors(
                            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic);
                        Console.Error.WriteLine($"[DEBUG] SmartFormatter has {ctors.Length} constructors:");
                        foreach (var ctor in ctors)
                        {
                            var ps = ctor.GetParameters();
                            Console.Error.WriteLine($"  ({string.Join(", ", ps.Select(p => $"{p.ParameterType.Name} {p.Name}"))})");
                        }
                        // Try the one with fewest params
                        var bestCtor = ctors.OrderBy(c => c.GetParameters().Length).First();
                        var args2 = bestCtor.GetParameters().Select(p =>
                            p.HasDefaultValue ? p.DefaultValue :
                            p.ParameterType.IsValueType ? Activator.CreateInstance(p.ParameterType) : null
                        ).ToArray();
                        var sf = bestCtor.Invoke(args2);
                        // Register extensions using the game's own LoadLocFormatters logic
                        // Call it via reflection on LocManager instance
                        try
                        {
                            var loadMethod = typeof(LocManager).GetMethod("LoadLocFormatters",
                                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                            if (loadMethod != null)
                            {
                                loadMethod.Invoke(instance, null);
                                Console.Error.WriteLine("[INFO] SmartFormatter initialized via LoadLocFormatters");
                            }
                            else
                            {
                                sfField.SetValue(null, sf);
                                Console.Error.WriteLine("[INFO] SmartFormatter set (no LoadLocFormatters found)");
                            }
                        }
                        catch (Exception lfEx)
                        {
                            sfField.SetValue(null, sf);
                            Console.Error.WriteLine($"[WARN] LoadLocFormatters failed: {lfEx.InnerException?.Message ?? lfEx.Message}");
                        }
                    }
                    catch (Exception sfEx)
                    {
                        Console.Error.WriteLine($"[WARN] SmartFormatter create failed: {sfEx.GetType().Name}: {sfEx.Message}");
                        if (sfEx.InnerException != null)
                            Console.Error.WriteLine($"  Inner: {sfEx.InnerException.GetType().Name}: {sfEx.InnerException.Message}");
                    }
                }
                else
                {
                    Console.Error.WriteLine("[WARN] _smartFormatter field not found in LocManager");
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] _smartFormatter init: {ex.GetType().Name}: {ex.Message}\n{ex.InnerException?.Message}"); }

            // Initialize _engTables to point to _tables (avoid null ref in fallback)
            try
            {
                var engTablesField = typeof(LocManager).GetField("_engTables",
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                engTablesField?.SetValue(instance, tables);
            }
            catch { }

            Console.Error.WriteLine("[INFO] LocManager initialized with stub tables");

            // Use Harmony to patch methods that need fallback behavior
            var harmony = new Harmony("sts2headless.locpatch");

            // With real loc data loaded, we only need fallback patches for:
            // 1. LocTable.GetRawText — return key for missing entries instead of throwing
            // 2. LocManager.SmartFormat — _smartFormatter is null, return raw text instead
            // We do NOT patch GetFormattedText/GetRawText on LocString anymore
            // so the real localization pipeline works (needed for Neow event etc.)

            var getRawText = typeof(LocTable).GetMethod("GetRawText",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public,
                null, new[] { typeof(string) }, null);
            var prefix = typeof(LocPatches).GetMethod(nameof(LocPatches.GetRawTextPrefix),
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (getRawText != null && prefix != null)
            {
                harmony.Patch(getRawText, new HarmonyMethod(prefix));
                Console.Error.WriteLine("[INFO] Patched LocTable.GetRawText");
            }

            // Patch GetLocString to not throw
            var getLocString = typeof(LocTable).GetMethod("GetLocString");
            var glsPrefix = typeof(LocPatches).GetMethod(nameof(LocPatches.GetLocStringPrefix),
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (getLocString != null && glsPrefix != null)
            {
                try { harmony.Patch(getLocString, new HarmonyMethod(glsPrefix)); }
                catch (Exception ex4) { Console.Error.WriteLine($"[WARN] Failed to patch GetLocString: {ex4.Message}"); }
            }

            // Patch FromChooseABundleScreen to use our card selector
            try
            {
                var bundleMethod = typeof(MegaCrit.Sts2.Core.Commands.CardSelectCmd).GetMethod("FromChooseABundleScreen",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                var bundlePrefix = typeof(LocPatches).GetMethod(nameof(LocPatches.BundleScreenPrefix),
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                if (bundleMethod != null && bundlePrefix != null)
                {
                    harmony.Patch(bundleMethod, new HarmonyMethod(bundlePrefix));
                    Console.Error.WriteLine("[INFO] Patched FromChooseABundleScreen");
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] Bundle patch: {ex.Message}"); }

            // Patch Neutralize.OnPlay to avoid NullRef in DamageCmd.Attack().Execute()
            try
            {
                var neutralizeType = typeof(MegaCrit.Sts2.Core.Models.Cards.Neutralize);
                var neutralizeOnPlay = neutralizeType.GetMethod("OnPlay",
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                if (neutralizeOnPlay != null)
                {
                    var neutPrefix = typeof(LocPatches).GetMethod(nameof(LocPatches.NeutralizePrefix),
                        System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
                    if (neutPrefix != null)
                    {
                        harmony.Patch(neutralizeOnPlay, new HarmonyMethod(neutPrefix));
                        Console.Error.WriteLine("[INFO] Patched Neutralize.OnPlay");
                    }
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] Neutralize patch: {ex.Message}"); }

            // Patch HasEntry to always return true
            PatchMethod(harmony, typeof(LocTable), "HasEntry", nameof(LocPatches.HasEntryPrefix));

            // Patch IsLocalKey to always return true
            PatchMethod(harmony, typeof(LocTable), "IsLocalKey", nameof(LocPatches.HasEntryPrefix));

            // Patch LocString.Exists (static) to always return true
            var locStringExists = typeof(LocString).GetMethod("Exists",
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (locStringExists != null)
            {
                PatchMethod(harmony, locStringExists, nameof(LocPatches.HasEntryPrefix));
            }

            // Patch LocTable.GetLocStringsWithPrefix to return empty list
            PatchMethod(harmony, typeof(LocTable), "GetLocStringsWithPrefix", nameof(LocPatches.GetLocStringsWithPrefixPrefix));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] InitLocManager failed: {ex.Message}");
        }
    }

    private static void PatchMethod(Harmony harmony, Type type, string methodName, string patchName)
    {
        try
        {
            var method = type.GetMethod(methodName, System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static);
            PatchMethod(harmony, method, patchName);
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] Failed to patch {type.Name}.{methodName}: {ex.Message}"); }
    }

    private static void PatchMethod(Harmony harmony, System.Reflection.MethodInfo? method, string patchName)
    {
        if (method == null) return;
        try
        {
            var prefix = typeof(LocPatches).GetMethod(patchName, System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public);
            if (prefix != null) harmony.Patch(method, new HarmonyMethod(prefix));
        }
        catch (Exception ex) { Console.Error.WriteLine($"[WARN] Failed to patch {method.Name}: {ex.Message}"); }
    }

    internal static class LocPatches
    {
        public static bool GetRawTextPrefix(LocTable __instance, string key, ref string __result)
        {
            // Return key as fallback "translation"
            __result = key;
            return false;
        }

        public static bool GetFormattedTextPrefix(LocString __instance, ref string __result)
        {
            __result = __instance?.LocEntryKey ?? "";
            return false;
        }

        public static bool GetRawTextInstancePrefix(LocString __instance, ref string __result)
        {
            __result = __instance?.LocEntryKey ?? "";
            return false;
        }


        /// <summary>Harmony prefix: replace Neutralize.OnPlay with safe damage+weak.</summary>
        public static bool NeutralizePrefix(CardModel __instance, ref Task __result,
            PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            if (cardPlay.Target == null) { __result = Task.CompletedTask; return false; }
            __result = NeutralizeSafe(__instance, choiceContext, cardPlay);
            return false;
        }

        private static async Task NeutralizeSafe(CardModel card, PlayerChoiceContext ctx, CardPlay play)
        {
            try
            {
                await CreatureCmd.Damage(ctx, play.Target!, card.DynamicVars.Damage.BaseValue,
                    MegaCrit.Sts2.Core.ValueProps.ValueProp.Move, card);
                await PowerCmd.Apply<WeakPower>(ctx, play.Target!, card.DynamicVars["WeakPower"].BaseValue,
                    card.Owner.Creature, card, false);
            }
            catch (Exception ex) { Console.Error.WriteLine($"[WARN] Neutralize safe: {ex.Message}"); }
        }

        public static bool HasEntryPrefix(ref bool __result)
        {
            __result = true;
            return false;
        }

        public static bool GetLocStringPrefix(LocTable __instance, string key, ref LocString __result)
        {
            var nameField = typeof(LocTable).GetField("_name",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            var tableName = nameField?.GetValue(__instance) as string ?? "_unknown";
            __result = new LocString(tableName, key);
            return false;
        }

        /// <summary>
        /// Intercept bundle selection — store bundles and wait for player to pick a pack index.
        /// </summary>
        public static bool BundleScreenPrefix(
            MegaCrit.Sts2.Core.Entities.Players.Player player,
            IReadOnlyList<IReadOnlyList<CardModel>> bundles,
            ref Task<IEnumerable<CardModel>> __result)
        {
            if (bundles.Count == 0)
            {
                __result = Task.FromResult<IEnumerable<CardModel>>(Array.Empty<CardModel>());
                return false;
            }

            // Store pending bundles for the main loop to present
            var sim = _bundleSimRef;
            if (sim != null)
            {
                sim._pendingBundles = bundles;
                sim._pendingBundleTcs = new TaskCompletionSource<IEnumerable<CardModel>>();
                Console.Error.WriteLine($"[SIM] Bundle selection pending: {bundles.Count} packs");

                __result = sim._pendingBundleTcs.Task;
                return false;
            }

            __result = Task.FromResult<IEnumerable<CardModel>>(bundles[0]);
            return false;
        }

        // Static reference so Harmony patch can access the simulator instance
        internal static RunSimulator? _bundleSimRef;

        public static bool GetLocStringsWithPrefixPrefix(ref IReadOnlyList<LocString> __result)
        {
            __result = new List<LocString>();
            return false;
        }
    }

    private static void Log(string message)
    {
        Console.Error.WriteLine($"[SIM] {message}");
    }

    private static Dictionary<string, object?> Error(string message) =>
        new() { ["type"] = "error", ["message"] = message };

    private static Dictionary<string, object?> ErrorWithTrace(string context, Exception ex)
    {
        var inner = ex;
        while (inner.InnerException != null) inner = inner.InnerException;
        return new Dictionary<string, object?>
        {
            ["type"] = "error",
            ["message"] = $"{context}: {inner.GetType().Name}: {inner.Message}",
            ["stack_trace"] = inner.StackTrace,
        };
    }

    private Dictionary<string, object?>? TryInspectCombatMembershipForError()
    {
        try
        {
            return BuildCombatMembershipDebug();
        }
        catch (Exception ex)
        {
            return new Dictionary<string, object?>
            {
                ["inspection_error"] = $"{ex.GetType().Name}: {ex.Message}",
            };
        }
    }

    private Dictionary<string, object?> BuildCombatMembershipDebug()
    {
        if (_runState == null)
            return new Dictionary<string, object?> { ["state"] = "no_run_state" };

        var player = _runState.Players.FirstOrDefault();
        var combatState = CombatManager.Instance.DebugOnlyGetState();
        var pcs = player?.PlayerCombatState;
        if (player == null || combatState == null || pcs == null)
            return new Dictionary<string, object?>
            {
                ["state"] = "combat_unavailable",
                ["has_player"] = player != null,
                ["has_combat_state"] = combatState != null,
                ["has_player_combat_state"] = pcs != null,
            };

        var containsMethod = combatState.GetType()
            .GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .FirstOrDefault(m =>
                m.Name == "Contains" &&
                m.GetParameters().Length == 1 &&
                m.GetParameters()[0].ParameterType.Name.Contains("AbstractModel", StringComparison.Ordinal));

        bool? ContainsModel(object? model)
        {
            if (containsMethod == null || model == null)
                return null;
            try
            {
                return (bool?)containsMethod.Invoke(combatState, new[] { model });
            }
            catch
            {
                return null;
            }
        }

        static Dictionary<string, object?> Entry(string kind, string id, bool? contains, string? owner = null)
        {
            return new Dictionary<string, object?>
            {
                ["kind"] = kind,
                ["id"] = id,
                ["owner"] = owner,
                ["contained"] = contains,
            };
        }

        var missing = new List<Dictionary<string, object?>>();

        void AddIfMissing(string kind, string id, object? model, string? owner = null)
        {
            var contains = ContainsModel(model);
            if (contains != true)
                missing.Add(Entry(kind, id, contains, owner));
        }

        AddIfMissing("player_creature", player.Character?.Id.Entry ?? "PLAYER", player.Creature);

        foreach (var relic in player.Relics ?? Enumerable.Empty<RelicModel>())
            AddIfMissing("relic", relic.Id.Entry, relic);

        foreach (var power in player.Creature?.Powers ?? Enumerable.Empty<PowerModel>())
            AddIfMissing("player_power", power.Id.Entry, power, player.Character?.Id.Entry ?? "PLAYER");

        foreach (var enemy in combatState.Enemies ?? Enumerable.Empty<Creature>())
        {
            var enemyId = enemy.Monster?.Id.Entry ?? enemy.Name ?? "UNKNOWN_ENEMY";
            AddIfMissing("enemy_creature", enemyId, enemy);
            foreach (var power in enemy.Powers ?? Enumerable.Empty<PowerModel>())
                AddIfMissing("enemy_power", power.Id.Entry, power, enemyId);
        }

        foreach (var pile in pcs.AllPiles)
        {
            foreach (var card in pile.Cards)
            {
                var cardId = $"{card.Id.Entry}#{card.CurrentUpgradeLevel}";
                AddIfMissing($"card:{pile.Type}", cardId, card);
            }
        }

        var hookFieldSummary = combatState.GetType()
            .GetFields(BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)
            .Where(f => f.Name.Contains("hook", StringComparison.OrdinalIgnoreCase) ||
                        f.Name.Contains("listener", StringComparison.OrdinalIgnoreCase) ||
                        f.Name.Contains("subscriber", StringComparison.OrdinalIgnoreCase))
            .Select(f =>
            {
                object? value = null;
                int? count = null;
                try
                {
                    value = f.GetValue(combatState);
                    if (value is System.Collections.ICollection coll)
                        count = coll.Count;
                }
                catch { }
                return new Dictionary<string, object?>
                {
                    ["field"] = f.Name,
                    ["type"] = f.FieldType.FullName,
                    ["value_type"] = value?.GetType().FullName,
                    ["count"] = count,
                    ["is_null"] = value == null,
                };
            })
            .ToList();

        var drawPile = pcs.AllPiles.FirstOrDefault(p => string.Equals(p.Type.ToString(), "Draw", StringComparison.OrdinalIgnoreCase));
        var discardPile = pcs.AllPiles.FirstOrDefault(p => string.Equals(p.Type.ToString(), "Discard", StringComparison.OrdinalIgnoreCase));
        var enemyFieldList = AnyMember(combatState, "_enemies") as System.Collections.IList;
        var enemyFieldOrder = new List<Dictionary<string, object?>>();
        if (enemyFieldList != null)
        {
            foreach (var item in enemyFieldList)
            {
                if (item is not Creature creature)
                    continue;
                enemyFieldOrder.Add(new Dictionary<string, object?>
                {
                    ["monster_id"] = creature.Monster?.Id.Entry,
                    ["slot_name"] = creature.SlotName,
                    ["enemy_index"] = AnyMember(creature, "EnemyIndex") ?? AnyMember(creature, "_enemyIndex"),
                });
            }
        }
        var enemyPropertyOrder = (combatState.Enemies?.Where(e => e != null).Select(creature => new Dictionary<string, object?>
        {
            ["monster_id"] = creature.Monster?.Id.Entry,
            ["slot_name"] = creature.SlotName,
            ["enemy_index"] = AnyMember(creature, "EnemyIndex") ?? AnyMember(creature, "_enemyIndex"),
        }).Cast<object?>().ToList()) ?? new List<object?>();

        return new Dictionary<string, object?>
        {
            ["round"] = combatState.RoundNumber,
            ["side"] = combatState.CurrentSide.ToString(),
            ["contains_method_found"] = containsMethod != null,
            ["player_hp"] = player.Creature?.CurrentHp,
            ["enemy_count"] = combatState.Enemies?.Count ?? 0,
            ["hand_count"] = pcs.Hand?.Cards?.Count ?? 0,
            ["draw_count"] = drawPile?.Cards?.Count ?? 0,
            ["discard_count"] = discardPile?.Cards?.Count ?? 0,
            ["enemy_property_order"] = enemyPropertyOrder,
            ["enemy_field_order"] = enemyFieldOrder,
            ["missing_models"] = missing,
            ["hook_fields"] = hookFieldSummary,
        };
    }

    public Dictionary<string, object?> GetFullMap()
    {
        if (_runState?.Map == null)
            return Error("No map available");

        var map = _runState.Map;
        var rows = new List<List<Dictionary<string, object?>>>();
        var currentCoord = _runState.CurrentMapCoord;
        var visited = _runState.VisitedMapCoords;

        for (int row = 0; row < map.GetRowCount(); row++)
        {
            var rowNodes = new List<Dictionary<string, object?>>();
            foreach (var point in map.GetPointsInRow(row))
            {
                if (point == null) continue;
                var children = point.Children?.Select(ch => new Dictionary<string, object?>
                {
                    ["col"] = (int)ch.coord.col,
                    ["row"] = (int)ch.coord.row,
                }).ToList();

                var isVisited = visited?.Any(v => v.col == point.coord.col && v.row == point.coord.row) ?? false;
                var isCurrent = currentCoord.HasValue &&
                    currentCoord.Value.col == point.coord.col && currentCoord.Value.row == point.coord.row;

                rowNodes.Add(new Dictionary<string, object?>
                {
                    ["col"] = (int)point.coord.col,
                    ["row"] = (int)point.coord.row,
                    ["type"] = point.PointType.ToString(),
                    ["children"] = children,
                    ["visited"] = isVisited,
                    ["current"] = isCurrent,
                });
            }
            if (rowNodes.Count > 0)
                rows.Add(rowNodes);
        }

        // Boss node
        var bossNode = new Dictionary<string, object?>
        {
            ["col"] = (int)map.BossMapPoint.coord.col,
            ["row"] = (int)map.BossMapPoint.coord.row,
            ["type"] = map.BossMapPoint.PointType.ToString(),
        };

        // Add boss name/id — use BossEncounter?.Id?.Entry
        try
        {
            var bossIdEntry = _runState.Act?.BossEncounter?.Id?.Entry;
            if (!string.IsNullOrEmpty(bossIdEntry))
            {
                var monsterKey = bossIdEntry.EndsWith("_BOSS") ? bossIdEntry[..^5] : bossIdEntry;
                if (monsterKey == "THE_KIN") monsterKey = "KIN_PRIEST";
                bossNode["id"] = bossIdEntry;
                bossNode["name"] = _loc.Monster(monsterKey);
            }
        }
        catch { }

        return new Dictionary<string, object?>
        {
            ["type"] = "map",
            ["context"] = RunContext(),
            ["rows"] = rows,
            ["boss"] = bossNode,
            ["current_coord"] = currentCoord.HasValue ? new Dictionary<string, object?>
            {
                ["col"] = (int)currentCoord.Value.col,
                ["row"] = (int)currentCoord.Value.row,
            } : null,
        };
    }

    public void CleanUp(bool keepProcessAlive = false)
    {
        try
        {
            UnregisterCombatEventHandlers();
            if (RunManager.Instance.IsInProgress || RunManager.Instance.DebugOnlyGetState() != null)
                RunManager.Instance.CleanUp(graceful: true);
            if (keepProcessAlive)
                HardResetRunManagerForTest();
            _runState = null;
            _combatRewardsSet = null;
            _combatRewardsCompletion = null;
            _pendingCombatRewards.Clear();
            _activeCombatCardReward = null;
            _pendingBundles = null;
            _pendingBundleTcs = null;
            _pendingInteractionTask = null;
            _activeCrystalSphere = null;
            _cardSelector.CancelPending();
        }
        catch (Exception ex)
        {
            Log($"CleanUp exception: {ex.Message}");
        }
    }

    #endregion
}
