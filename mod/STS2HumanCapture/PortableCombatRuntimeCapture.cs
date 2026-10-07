using System.Collections;
using System.Reflection;
using System.Text.Json;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;

namespace STS2HumanCapture;

// Wire format mirrors RunSimulator.RuntimeValueSnapshot. Unknown live references
// reject capture instead of producing a root that can silently diverge later.
internal static class PortableCombatRuntimeCapture
{
    private const BindingFlags Members = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    private static readonly JsonSerializerOptions JsonOptions = new() { IncludeFields = true };

    internal sealed class Context(Player player, CombatState combat)
    {
        public Player Player { get; } = player;
        public CombatState Combat { get; } = combat;
        public Dictionary<object, int> Objects { get; } = new(ReferenceEqualityComparer.Instance);
        public Dictionary<object, int> HistoricalMonsterOwners { get; } = new(ReferenceEqualityComparer.Instance);
        public Dictionary<object, int> HistoricalCreatureOwners { get; } = new(ReferenceEqualityComparer.Instance);
        public Dictionary<object, (int OwnerId, string Path)> HistoricalMovePaths { get; } =
            new(ReferenceEqualityComparer.Instance);
        public Dictionary<object, MonsterModel> CompletedMoveOwners { get; } =
            new(ReferenceEqualityComparer.Instance);
        public int NextId { get; set; } = 1;
        public List<Creature> Enemies { get; } = combat.Enemies?.Where(e => e != null && e.IsAlive).ToList()
            ?? new List<Creature>();
    }

    private static Dictionary<string, object?> D() => new(StringComparer.Ordinal);
    private static string TypeName(Type type) => type.AssemblyQualifiedName ?? type.FullName ?? type.Name;
    private static string FullName(Type type) => type.FullName ?? type.Name;

    private static object? Member(object? value, string name)
    {
        if (value == null) return null;
        for (var type = value.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetField(name, Members | BindingFlags.DeclaredOnly);
            if (field != null) return field.GetValue(value);
            var property = type.GetProperty(name, Members | BindingFlags.DeclaredOnly);
            if (property != null) return property.GetValue(value);
        }
        return null;
    }

    internal static Dictionary<string, object?>? PrimitiveState(object? instance, int ordinal = 0)
    {
        if (instance == null) return null;
        var fields = new List<Dictionary<string, object?>>();
        for (var type = instance.GetType(); type != null; type = type.BaseType)
        foreach (var field in type.GetFields(Members | BindingFlags.DeclaredOnly))
        {
            if (field.IsInitOnly || !Scalar(field.FieldType)) continue;
            var value = field.GetValue(instance);
            fields.Add(new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["DeclaringType"] = FullName(type), ["Name"] = field.Name,
                ["FieldType"] = TypeName(field.FieldType),
                ["Value"] = value is Enum ? value.ToString() : value,
            });
        }
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["TypeName"] = FullName(instance.GetType()), ["TypeOrdinal"] = ordinal,
            ["Fields"] = fields,
        };
    }

    internal static List<Dictionary<string, object?>> HookStates(CombatState combat)
    {
        var method = combat.GetType().GetMethod("IterateHookListeners", Members)
            ?? throw new InvalidOperationException("Combat hook listener enumerator is unavailable");
        if (method.Invoke(combat, []) is not IEnumerable listeners)
            throw new InvalidOperationException("Combat hook listener inventory is unavailable");
        var result = new List<Dictionary<string, object?>>();
        var ordinals = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var listener in listeners)
        {
            if (listener == null) continue;
            var name = FullName(listener.GetType());
            var ordinal = ordinals.GetValueOrDefault(name);
            ordinals[name] = ordinal + 1;
            var state = PrimitiveState(listener, ordinal)!;
            if (((ICollection)state["Fields"]!).Count > 0) result.Add(state);
        }
        return result;
    }

    internal static List<Dictionary<string, object?>> History(Context context)
    {
        var history = CombatManager.Instance.History
            ?? throw new InvalidOperationException("Combat history is unavailable");
        if (Member(history, "Entries") is not IEnumerable entries)
            throw new InvalidOperationException("Combat history entries are unavailable");
        var entryList = entries.Cast<object?>().Where(entry => entry != null).Cast<object>().ToList();
        foreach (var entry in entryList)
        {
            if (entry.GetType().Name == "MonsterPerformedMoveEntry"
                && Member(entry, "Move") is object move
                && Member(entry, "Monster") is MonsterModel owner)
                context.CompletedMoveOwners[move] = owner;
        }
        var result = new List<Dictionary<string, object?>>();
        foreach (var entry in entryList)
            result.Add(Value(entry, context, 0, $"CombatHistoryEntries[{result.Count}]"));
        return result;
    }

    internal static List<Dictionary<string, object?>> PowerRefs(Context context)
    {
        var creatures = new List<Creature> { context.Player.Creature };
        creatures.AddRange(context.Enemies);
        var result = new List<Dictionary<string, object?>>();
        for (var creatureIndex = 0; creatureIndex < creatures.Count; creatureIndex++)
        {
            var powers = creatures[creatureIndex].Powers.ToList();
            for (var powerIndex = 0; powerIndex < powers.Count; powerIndex++)
            {
                var power = powers[powerIndex];
                var path = $"ActivePowerRefs[{creatureIndex},{powerIndex}]";
                var vars = new List<Dictionary<string, object?>>();
                foreach (var variable in power.DynamicVars.Values)
                    vars.Add(new Dictionary<string, object?>(StringComparer.Ordinal)
                    {
                        ["Name"] = variable.Name,
                        ["State"] = PrimitiveState(variable)
                            ?? throw new InvalidOperationException($"Missing dynamic variable at {path}"),
                    });
                result.Add(new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["CreatureIndex"] = creatureIndex - 1, ["PowerIndex"] = powerIndex,
                    ["PowerId"] = power.Id.Entry,
                    ["Applier"] = Value(Member(power, "_applier"), context, 0, path + "._applier"),
                    ["Target"] = Value(Member(power, "_target"), context, 0, path + "._target"),
                    ["InternalData"] = Value(Member(power, "_internalData"), context, 0, path + "._internalData"),
                    ["DynamicVars"] = vars,
                });
            }
        }
        return result;
    }

    private static bool Scalar(Type type) => type.IsEnum || type == typeof(string) || type == typeof(decimal)
        || type == typeof(bool) || type == typeof(char)
        || type == typeof(byte) || type == typeof(sbyte)
        || type == typeof(short) || type == typeof(ushort)
        || type == typeof(int) || type == typeof(uint)
        || type == typeof(long) || type == typeof(ulong)
        || type == typeof(float) || type == typeof(double);

    private static Dictionary<string, object?>? RngState(string name, object? rng)
    {
        if (rng == null) return null;
        var random = Member(rng, "_random");
        var prng = Member(Member(random, "_impl"), "_prng");
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["Name"] = name,
            ["Counter"] = Convert.ToInt32(Member(rng, "<Counter>k__BackingField") ?? 0),
            ["Seed"] = Member(rng, "<Seed>k__BackingField"),
            ["S0"] = Member(random, "_s0"), ["S1"] = Member(random, "_s1"),
            ["S2"] = Member(random, "_s2"), ["S3"] = Member(random, "_s3"),
            ["Inext"] = Member(prng, "_inext"), ["Inextp"] = Member(prng, "_inextp"),
            ["SeedArray"] = (Member(prng, "_seedArray") as int[])?.ToList(),
        };
    }

    private static Dictionary<string, object?> CreatureState(Creature creature) =>
        new(StringComparer.Ordinal)
        {
            ["MonsterId"] = creature.Monster?.Id.Entry ?? creature.Name ?? "UNKNOWN",
            ["SlotName"] = creature.SlotName,
            ["CurrentHp"] = creature.CurrentHp, ["MaxHp"] = creature.MaxHp,
            ["Block"] = creature.Block,
            ["MonsterMaxHpBeforeModification"] = Member(creature, "MonsterMaxHpBeforeModification"),
            ["CombatId"] = Member(creature, "CombatId"),
            ["SpawnedThisTurn"] = Member(creature.Monster, "_spawnedThisTurn"),
            ["MonsterRng"] = RngState("Monster", Member(creature.Monster, "_rng")),
            ["Powers"] = creature.Powers.Select(power => new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["Id"] = power.Id.Entry, ["Amount"] = power.Amount,
                ["AmountOnTurnStart"] = Member(power, "_amountOnTurnStart"),
            }).ToList(),
        };

    private static IEnumerable<(string Path, object Value)> MoveGraph(MonsterModel monster)
    {
        var machine = monster.MoveStateMachine;
        var queue = new Queue<(string Path, object? Value)>();
        queue.Enqueue(("next", monster.NextMove));
        queue.Enqueue(("current", Member(machine, "_currentState")));
        queue.Enqueue(("initial", Member(machine, "_initialState")));
        if (Member(machine, "States") is IDictionary states)
            foreach (DictionaryEntry entry in states) queue.Enqueue(($"states/{entry.Key}", entry.Value));
        if (Member(machine, "StateLog") is IEnumerable log)
        {
            var index = 0;
            foreach (var item in log) queue.Enqueue(($"log/{index++}", item));
        }
        var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);
        while (queue.Count > 0)
        {
            var (path, value) = queue.Dequeue();
            if (value == null || !seen.Add(value)) continue;
            yield return (path, value);
            queue.Enqueue(($"{path}/followup", Member(value, "FollowUpState")));
            if (Member(value, "States") is IEnumerable branches)
            {
                var index = 0;
                foreach (var branch in branches) queue.Enqueue(($"{path}/branches/{index++}", branch));
            }
        }
    }

    private static Dictionary<string, object?> EnemyAiState(Creature creature)
    {
        var monster = creature.Monster!;
        var machine = monster.MoveStateMachine;
        var reverse = new Dictionary<object, string>(ReferenceEqualityComparer.Instance);
        if (Member(machine, "States") is IDictionary states)
            foreach (DictionaryEntry entry in states)
                if (entry.Value != null) reverse[entry.Value] = entry.Key?.ToString() ?? "";
        string? StateId(object? value) => value == null ? null
            : reverse.TryGetValue(value, out var id) && !string.IsNullOrWhiteSpace(id) ? id
            : Member(value, "Id")?.ToString() ?? Member(value, "StateId")?.ToString();
        var log = (Member(machine, "StateLog") as IEnumerable)?.Cast<object?>().ToList()
            ?? new List<object?>();
        var current = Member(machine, "_currentState") ?? Member(machine, "CurrentState");
        var initial = Member(machine, "_initialState") ?? Member(machine, "InitialState");
        var next = Member(monster, "NextMove");
        var moves = new Dictionary<string, object?>(StringComparer.Ordinal);
        void CaptureMove(object? value)
        {
            var id = StateId(value);
            if (value == null || string.IsNullOrWhiteSpace(id) || moves.ContainsKey(id)) return;
            var intents = (Member(value, "Intents") as IEnumerable)?.Cast<object?>()
                .Where(item => item != null).Cast<object>().ToList() ?? new List<object>();
            var follow = Member(value, "FollowUpState");
            moves[id] = new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["StateId"] = id, ["FollowUpStateId"] = Member(value, "FollowUpStateId")?.ToString(),
                ["ResolvedFollowUpStateId"] = StateId(follow),
                ["MustPerformOnceBeforeTransitioning"] = Member(value, "MustPerformOnceBeforeTransitioning") is true,
                ["PerformedAtLeastOnce"] = Member(value, "_performedAtLeastOnce") is true,
                ["IntentTypeNames"] = intents.Select(item => FullName(item.GetType())).ToList(),
            };
            CaptureMove(follow);
        }
        CaptureMove(current); CaptureMove(initial); CaptureMove(next);
        foreach (var item in log) CaptureMove(item);
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["MonsterId"] = monster.Id.Entry, ["CurrentStateId"] = StateId(current),
            ["InitialStateId"] = StateId(initial), ["NextMoveId"] = StateId(next),
            ["PerformedFirstMove"] = Member(machine, "_performedFirstMove"),
            ["StateLogIds"] = log.Select(StateId).Where(id => !string.IsNullOrWhiteSpace(id)).ToList(),
            ["MoveStates"] = moves,
        };
    }

    private static Dictionary<string, object?> Value(object? value, Context context, int depth, string path,
                                                       bool historicalInactiveRuntime = false)
    {
        var result = D();
        if (value == null) { result["Kind"] = "null"; return result; }
        var type = value.GetType();
        result["TypeName"] = TypeName(type);
        if (value is Delegate || type == typeof(IntPtr) || type == typeof(UIntPtr)
            || type.IsPointer || type.IsFunctionPointer)
            throw new InvalidOperationException($"Unsupported callback or pointer at {path}: {FullName(type)}");
        if (Scalar(type))
        {
            result["Kind"] = "scalar";
            result["ScalarJson"] = JsonSerializer.Serialize(value is Enum ? value.ToString() : value, JsonOptions);
            return result;
        }
        if (ReferenceEquals(value, CombatManager.Instance.History)) { result["Kind"] = "combat_history"; return result; }
        if (ReferenceEquals(value, context.Combat)) { result["Kind"] = "combat_state"; return result; }
        if (value is CombatState) throw new InvalidOperationException($"Noncanonical combat state at {path}");
        if (ReferenceEquals(value, context.Player)) { result["Kind"] = "player"; return result; }
        if (value is Player) throw new InvalidOperationException($"Noncanonical player at {path}");
        if (value is PotionModel potion)
        {
            var slots = context.Player.Potions.ToList();
            var index = slots.FindIndex(item => ReferenceEquals(item, potion));
            if (index >= 0)
            {
                result["Kind"] = "potion"; result["CardIndex"] = index;
                result["ModelId"] = potion.Id.Entry; return result;
            }
        }
        if (value is RelicModel relic)
        {
            var relics = context.Player.Relics.ToList();
            var index = relics.FindIndex(item => ReferenceEquals(item, relic));
            if (index >= 0)
            {
                result["Kind"] = "relic"; result["CardIndex"] = index;
                result["ModelId"] = relic.Id.Entry; return result;
            }
        }
        if (value is Creature creature)
        {
            var index = context.Enemies.FindIndex(item => ReferenceEquals(item, creature));
            var isPlayer = ReferenceEquals(context.Player.Creature, creature);
            if (!isPlayer && index < 0)
            {
                if (context.Objects.TryGetValue(creature, out var existingId))
                { result["Kind"] = "ref"; result["RefId"] = existingId; return result; }
                var ownerId = context.NextId++;
                context.Objects[creature] = ownerId;
                context.HistoricalCreatureOwners[creature] = ownerId;
                if (creature.Monster == null)
                    throw new InvalidOperationException($"Historical creature has no monster at {path}");
                context.HistoricalMonsterOwners[creature.Monster] = ownerId;
                foreach (var (movePath, move) in MoveGraph(creature.Monster))
                    context.HistoricalMovePaths.TryAdd(move, (ownerId, movePath));
                var creatureFields = new List<Dictionary<string, object?>>();
                for (var current = type; current != null; current = current.BaseType)
                foreach (var field in current.GetFields(Members | BindingFlags.DeclaredOnly))
                {
                    if (field.IsStatic || typeof(MonsterModel).IsAssignableFrom(field.FieldType)
                        || typeof(Delegate).IsAssignableFrom(field.FieldType)) continue;
                    creatureFields.Add(new Dictionary<string, object?>(StringComparer.Ordinal)
                    {
                        ["DeclaringType"] = FullName(current), ["Name"] = field.Name,
                        ["Value"] = Value(field.GetValue(creature), context, depth + 1,
                            $"{path}.{field.Name}", historicalInactiveRuntime: true),
                    });
                }
                result["Kind"] = "historical_creature"; result["ObjectId"] = ownerId;
                result["ModelId"] = creature.Monster.Id.Entry; result["SlotName"] = creature.SlotName;
                result["HistoricalCreatureStateJson"] = JsonSerializer.Serialize(CreatureState(creature), JsonOptions);
                result["HistoricalEnemyAiJson"] = JsonSerializer.Serialize(EnemyAiState(creature), JsonOptions);
                result["HistoricalMonsterRngJson"] = JsonSerializer.Serialize(
                    RngState("HistoricalMonster", Member(creature.Monster, "_rng")), JsonOptions);
                result["HistoricalHadMoveStateMachine"] = creature.Monster.MoveStateMachine != null;
                result["Fields"] = creatureFields;
                return result;
            }
            result["Kind"] = "creature"; result["IsPlayerCreature"] = isPlayer;
            result["CreatureIndex"] = index >= 0 ? index : null;
            result["ModelId"] = creature.Monster?.Id.Entry; return result;
        }
        if (value is MonsterModel monster)
        {
            if (context.HistoricalMonsterOwners.TryGetValue(monster, out var ownerId))
            {
                result["Kind"] = "historical_monster"; result["RefId"] = ownerId;
                result["ModelId"] = monster.Id.Entry; return result;
            }
            var index = context.Enemies.FindIndex(item => ReferenceEquals(item.Monster, monster));
            if (index < 0) throw new InvalidOperationException($"Detached historical monster at {path}");
            result["Kind"] = "monster"; result["CreatureIndex"] = index;
            result["ModelId"] = monster.Id.Entry; return result;
        }
        if (type.Name == "MoveState")
        {
            if (context.Objects.TryGetValue(value, out var existingMoveId))
            { result["Kind"] = "ref"; result["RefId"] = existingMoveId; return result; }
            if (context.HistoricalMovePaths.TryGetValue(value, out var historicalMove))
            {
                result["Kind"] = "historical_move_state"; result["RefId"] = historicalMove.OwnerId;
                result["MovePath"] = historicalMove.Path;
                result["ModelId"] = Member(value, "StateId")?.ToString() ?? Member(value, "Id")?.ToString();
                return result;
            }
            var stateId = Member(value, "StateId")?.ToString() ?? Member(value, "Id")?.ToString();
            for (var i = 0; i < context.Enemies.Count; i++)
            {
                var activeMonster = context.Enemies[i].Monster;
                var machine = activeMonster?.MoveStateMachine;
                var states = Member(machine, "States") as IDictionary;
                var found = ReferenceEquals(activeMonster?.NextMove, value)
                    || ReferenceEquals(Member(machine, "_currentState"), value)
                    || (states?.Values.Cast<object?>().Any(item => ReferenceEquals(item, value)) ?? false)
                    || ((Member(machine, "StateLog") as IEnumerable)?.Cast<object?>()
                        .Any(item => ReferenceEquals(item, value)) ?? false);
                if (!found) continue;
                result["Kind"] = "enemy_move_state"; result["CreatureIndex"] = i;
                result["ModelId"] = stateId; return result;
            }
            if (context.CompletedMoveOwners.TryGetValue(value, out var completedOwner)
                && !string.IsNullOrWhiteSpace(stateId))
            {
                var canonical = ModelDb.GetById<MonsterModel>(completedOwner.Id);
                var fresh = canonical?.ToMutable()
                    ?? throw new InvalidOperationException($"Cannot construct native move owner at {path}");
                fresh.SetUpForCombat();
                var nativeCount = MoveGraph(fresh).Count(item =>
                    (Member(item.Value, "StateId")?.ToString() ?? Member(item.Value, "Id")?.ToString()) == stateId);
                if (nativeCount == 1)
                {
                    var ownerIndex = context.Enemies.FindIndex(enemy =>
                        ReferenceEquals(enemy.Monster, completedOwner));
                    var historicalOwnerId = context.HistoricalMonsterOwners.TryGetValue(
                        completedOwner, out var detachedId) ? detachedId : (int?)null;
                    if (ownerIndex < 0 && historicalOwnerId == null)
                        throw new InvalidOperationException($"Completed historical move lacks an owner at {path}");
                    var completedId = context.NextId++;
                    context.Objects[value] = completedId;
                    result["Kind"] = "completed_move_state";
                    result["ObjectId"] = completedId;
                    result["CreatureIndex"] = ownerIndex >= 0 ? ownerIndex : null;
                    result["RefId"] = ownerIndex >= 0 ? null : historicalOwnerId;
                    result["OwnerModelId"] = completedOwner.Id.Entry;
                    result["ModelId"] = stateId;
                    result["MovePrimitiveState"] = PrimitiveState(value);
                    return result;
                }
            }
            historicalInactiveRuntime = true;
        }
        if (value is CardModel card)
        {
            foreach (var pile in context.Player.PlayerCombatState.AllPiles)
            {
                var cards = pile.Cards.ToList();
                var index = cards.FindIndex(item => ReferenceEquals(item, card));
                if (index < 0) continue;
                result["Kind"] = "card"; result["PileType"] = Convert.ToInt32(pile.Type);
                result["CardIndex"] = index; result["ModelId"] = card.Id.Entry; return result;
            }
            if (context.Objects.TryGetValue(card, out var oldCardId))
            { result["Kind"] = "ref"; result["RefId"] = oldCardId; return result; }
            var cardId = context.NextId++;
            context.Objects[card] = cardId;
            result["Kind"] = "historical_card"; result["ObjectId"] = cardId;
            result["ModelId"] = card.Id.Entry;
            result["NativeJson"] = JsonSerializer.Serialize(card.ToSerializable(), JsonOptions);
            return result;
        }
        if (type.FullName == "MegaCrit.Sts2.Core.Localization.DynamicVars.CalculatedDamageVar")
        {
            foreach (var pile in context.Player.PlayerCombatState.AllPiles)
            {
                var cards = pile.Cards.ToList();
                for (var index = 0; index < cards.Count; index++)
                {
                    object? calculated;
                    try { calculated = cards[index].DynamicVars.CalculatedDamage; }
                    catch (KeyNotFoundException) { continue; }
                    if (!ReferenceEquals(calculated, value)) continue;
                    result["Kind"] = "card_calculated_damage_var";
                    result["PileType"] = Convert.ToInt32(pile.Type);
                    result["CardIndex"] = index;
                    result["ModelId"] = cards[index].Id.Entry;
                    return result;
                }
            }
            throw new InvalidOperationException($"Detached calculated damage variable at {path}");
        }
        if (value is PowerModel power)
        {
            var creatures = new List<Creature> { context.Player.Creature };
            creatures.AddRange(context.Enemies);
            for (var i = 0; i < creatures.Count; i++)
            {
                var powers = creatures[i].Powers.ToList();
                var index = powers.FindIndex(item => ReferenceEquals(item, power));
                if (index < 0) continue;
                result["Kind"] = "power"; result["IsPlayerCreature"] = i == 0;
                result["CreatureIndex"] = i == 0 ? null : i - 1;
                result["PowerIndex"] = index; result["ModelId"] = power.Id.Entry;
                return result;
            }
            foreach (var (historicalCreature, ownerId) in context.HistoricalCreatureOwners)
            {
                if (historicalCreature is not Creature departed) continue;
                var powers = departed.Powers.ToList();
                var index = powers.FindIndex(item => ReferenceEquals(item, power));
                if (index < 0) continue;
                result["Kind"] = "historical_power"; result["RefId"] = ownerId;
                result["PowerIndex"] = index; result["ModelId"] = power.Id.Entry;
                return result;
            }
            // Removed powers can remain referenced by combat history. Match the
            // headless exporter: serialize their object fields below, preserving
            // shared references, instead of requiring an active power slot.
        }
        if (context.Objects.TryGetValue(value, out var oldId))
        { result["Kind"] = "ref"; result["RefId"] = oldId; return result; }
        if (depth >= 12)
            throw new InvalidOperationException($"Runtime capture exceeded depth at {path}: {FullName(type)}");
        var objectId = context.NextId++;
        context.Objects[value] = objectId;
        result["ObjectId"] = objectId;
        if (value is IDictionary dictionary)
        {
            result["Kind"] = "dictionary";
            var entries = new List<Dictionary<string, object?>>();
            foreach (DictionaryEntry entry in dictionary)
            {
                var index = entries.Count;
                entries.Add(new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["Key"] = Value(entry.Key, context, depth + 1, $"{path}.Entries[{index}].Key", historicalInactiveRuntime),
                    ["Value"] = Value(entry.Value, context, depth + 1, $"{path}.Entries[{index}].Value", historicalInactiveRuntime),
                });
            }
            result["Entries"] = entries; return result;
        }
        if (value is IEnumerable enumerable)
        {
            result["Kind"] = "list";
            var items = new List<Dictionary<string, object?>>();
            foreach (var item in enumerable)
                items.Add(Value(item, context, depth + 1, $"{path}.Items[{items.Count}]", historicalInactiveRuntime));
            result["Items"] = items; return result;
        }
        result["Kind"] = "object";
        var fields = new List<Dictionary<string, object?>>();
        var reflected = new List<FieldInfo>();
        for (var current = type; current != null; current = current.BaseType)
            reflected.AddRange(current.GetFields(Members | BindingFlags.DeclaredOnly));
        if (type.BaseType?.Name == "CombatHistoryEntry" || type.Name.EndsWith("Entry", StringComparison.Ordinal))
            reflected = reflected.OrderBy(field => field.Name == "<Actor>k__BackingField" ? 0 : 1).ToList();
        foreach (var field in reflected)
        {
            if (field.IsStatic || (field.DeclaringType?.FullName == "MegaCrit.Sts2.Core.Commands.Builders.AttackCommand"
                && field.Name is "_customAttackerVfxNodes" or "_customHitVfxNodes")
                || (historicalInactiveRuntime && field.DeclaringType?.Assembly == typeof(CardModel).Assembly
                    && ((field.DeclaringType.Name == "MoveState" && field.Name == "_onPerform")
                        || (field.DeclaringType.Namespace?.StartsWith(
                            "MegaCrit.Sts2.Core.MonsterMoves.Intents", StringComparison.Ordinal) == true
                            && field.Name == "<DamageCalc>k__BackingField")
                        || (field.DeclaringType.FullName?.StartsWith(
                            "MegaCrit.Sts2.Core.MonsterMoves.", StringComparison.Ordinal) == true
                            && field.Name == "weightLambda")))) continue;
            fields.Add(new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["DeclaringType"] = FullName(field.DeclaringType ?? type),
                ["Name"] = field.Name,
                ["Value"] = Value(field.GetValue(value), context, depth + 1,
                    $"{path}.{field.Name}", historicalInactiveRuntime),
            });
        }
        result["Fields"] = fields;
        return result;
    }
}
