using System.Collections;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Multiplayer;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Multiplayer.Serialization;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;

namespace STS2HumanCapture;

internal sealed record StoredCombatSnapshot(
    string Id,
    string Schema,
    string CapturedAtUtc,
    string Sha256,
    int Bytes,
    string Json);

internal static class AuthoritativeCombatSnapshots
{
    public const string Schema = "sts2.combat_snapshot.headless.v3";
    private const int Capacity = 128;
    private static readonly object Gate = new();
    private static readonly Dictionary<string, StoredCombatSnapshot> Snapshots = new(StringComparer.Ordinal);
    private static readonly Queue<string> Order = new();
    private static readonly JsonSerializerOptions SnapshotJsonOptions = new()
    {
        IncludeFields = true,
        WriteIndented = false,
    };

    public static SortedDictionary<string, object?> CaptureForEvent()
    {
        if (!CombatManager.Instance.IsInProgress)
        {
            return new(StringComparer.Ordinal)
            {
                ["status"] = "not_combat",
                ["schema"] = Schema,
            };
        }

        try
        {
            var stored = Capture();
            lock (Gate)
            {
                Snapshots[stored.Id] = stored;
                Order.Enqueue(stored.Id);
                while (Order.Count > Capacity)
                {
                    var expired = Order.Dequeue();
                    Snapshots.Remove(expired);
                }
            }
            return new(StringComparer.Ordinal)
            {
                ["status"] = "complete",
                ["schema"] = stored.Schema,
                ["snapshot_id"] = stored.Id,
                ["captured_at_utc"] = stored.CapturedAtUtc,
                ["sha256"] = stored.Sha256,
                ["bytes"] = stored.Bytes,
            };
        }
        catch (Exception ex)
        {
            return new(StringComparer.Ordinal)
            {
                ["status"] = "error",
                ["schema"] = Schema,
                ["error_type"] = ex.GetType().Name,
                ["error"] = ex.Message,
            };
        }
    }

    public static bool TryGet(string snapshotId, out StoredCombatSnapshot snapshot)
    {
        lock (Gate)
            return Snapshots.TryGetValue(snapshotId, out snapshot!);
    }

    private static StoredCombatSnapshot Capture()
    {
        var runState = RunManager.Instance.DebugOnlyGetState()
            ?? throw new InvalidOperationException("No run in progress");
        var room = runState.CurrentRoom as CombatRoom
            ?? throw new InvalidOperationException("Current room is not combat");
        var player = runState.Players.FirstOrDefault()
            ?? throw new InvalidOperationException("Combat player is unavailable");
        var combatState = CombatManager.Instance.DebugOnlyGetState()
            ?? throw new InvalidOperationException("Combat state is unavailable");
        var netState = NetFullCombatState.FromRun(runState, justFinishedAction: null!)
            ?? throw new InvalidOperationException("NetFullCombatState.FromRun returned null");

        var runRng = CaptureDetailedRngStates(runState.Rng);
        var playerRng = CaptureDetailedRngStates(player.PlayerRng);
        if (!RngStatesComplete(runRng) || !RngStatesComplete(playerRng))
            throw new InvalidOperationException("Complete run and player RNG states are required");

        var id = $"combat_{Guid.NewGuid():N}";
        var capturedAt = DateTime.UtcNow.ToString("O");
        var enemies = combatState.Enemies?.Where(enemy => enemy != null && enemy.IsAlive).ToList()
            ?? new List<Creature>();
        var runtime = new PortableCombatRuntimeCapture.Context(player, combatState);
        var history = PortableCombatRuntimeCapture.History(runtime);
        var powerRefs = PortableCombatRuntimeCapture.PowerRefs(runtime);
        var hookStates = PortableCombatRuntimeCapture.HookStates(combatState);
        var playerCombatState = PortableCombatRuntimeCapture.PrimitiveState(player.PlayerCombatState)
            ?? throw new InvalidOperationException("Player combat state is unavailable");
        var playerExtraState = PortableCombatRuntimeCapture.PrimitiveState(player.ExtraFields)
            ?? throw new InvalidOperationException("Player extra state is unavailable");
        var envelope = new SortedDictionary<string, object?>(StringComparer.Ordinal)
        {
            ["Id"] = id,
            ["CharacterName"] = player.Character?.Id.Entry ?? "IRONCLAD",
            ["AscensionLevel"] = runState.AscensionLevel,
            ["Seed"] = ReadMember(ReadMember(netState, "Rng"), "Seed")?.ToString() ?? "restored",
            ["RoomJson"] = JsonSerializer.Serialize(room.ToSerializable(), SnapshotJsonOptions),
            ["PlayerJson"] = JsonSerializer.Serialize(player.ToSerializable(), SnapshotJsonOptions),
            ["NetState"] = BuildPlainNetState(netState, player),
            ["EnemyCreatureStates"] = CaptureEnemyCreatureStates(enemies),
            ["EnemyAiStates"] = CaptureEnemyAiStates(enemies),
            ["RunRngStates"] = runRng,
            ["PlayerRngStates"] = playerRng,
            ["RoundNumber"] = combatState.RoundNumber,
            ["CurrentSide"] = combatState.CurrentSide,
            ["RelicStates"] = CaptureRelicStates(player),
            ["HookStates"] = hookStates,
            ["PlayerCombatState"] = playerCombatState,
            ["PlayerExtraState"] = playerExtraState,
            ["CombatHistoryEntries"] = history,
            ["ActivePowerRefs"] = powerRefs,
        };
        var json = JsonSerializer.Serialize(envelope, SnapshotJsonOptions);
        var bytes = Encoding.UTF8.GetBytes(json);
        var sha256 = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
        return new StoredCombatSnapshot(id, Schema, capturedAt, sha256, bytes.Length, json);
    }

    private static bool RngStatesComplete(IReadOnlyList<SortedDictionary<string, object?>> states) =>
        states.Count > 0 && states.All(state =>
            state.GetValueOrDefault("Seed") != null
            && state.GetValueOrDefault("S0") != null
            && state.GetValueOrDefault("S1") != null
            && state.GetValueOrDefault("S2") != null
            && state.GetValueOrDefault("S3") != null);

    private static SortedDictionary<string, object?> BuildPlainNetState(object netState, Player player)
    {
        var livePiles = Enumerate(ReadMember(player.PlayerCombatState, "AllPiles"))
            .Where(pile => pile != null)
            .ToDictionary(pile => Convert.ToInt32(ReadMember(pile, "Type")),
                pile => Enumerate(ReadMember(pile, "Cards")).Where(card => card != null).ToList());
        var creatures = Enumerate(ReadMember(netState, "Creatures"))
            .Where(value => value != null)
            .Select(value => new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["monsterId"] = PlainModelId(ReadMember(value, "monsterId") ?? ReadMember(value, "MonsterId")),
                ["playerId"] = NullableUInt64(ReadMember(value, "playerId")),
                ["currentHp"] = Convert.ToInt32(ReadMember(value, "currentHp") ?? 0),
                ["maxHp"] = Convert.ToInt32(ReadMember(value, "maxHp") ?? 0),
                ["block"] = Convert.ToInt32(ReadMember(value, "block") ?? 0),
                ["powers"] = Enumerate(ReadMember(value, "powers"))
                    .Where(power => power != null)
                    .Select(power => new SortedDictionary<string, object?>(StringComparer.Ordinal)
                    {
                        ["id"] = PlainModelId(ReadMember(power, "id")),
                        ["amount"] = Convert.ToInt32(ReadMember(power, "amount") ?? 0),
                    })
                    .Where(power => power["id"] is IDictionary<string, object?> id
                        && id.TryGetValue("Entry", out var entry)
                        && !string.IsNullOrWhiteSpace(entry?.ToString()))
                    .ToList(),
            })
            .ToList();

        var players = Enumerate(ReadMember(netState, "Players"))
            .Where(value => value != null)
            .Select(value => new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["playerId"] = NullableUInt64(ReadMember(value, "playerId")),
                ["characterId"] = PlainModelId(ReadMember(value, "characterId")),
                ["energy"] = Convert.ToInt32(ReadMember(value, "energy") ?? 0),
                ["stars"] = Convert.ToInt32(ReadMember(value, "stars") ?? 0),
                ["maxStars"] = Convert.ToInt32(ReadMember(value, "maxStars") ?? 0),
                ["maxPotionCount"] = Convert.ToInt32(ReadMember(value, "maxPotionCount") ?? 0),
                ["gold"] = Convert.ToInt32(ReadMember(value, "gold") ?? 0),
                ["piles"] = Enumerate(ReadMember(value, "piles"))
                    .Where(pile => pile != null)
                    .Select(pile => BuildPlainPile(pile!, livePiles)).ToList(),
                ["rngSet"] = new SortedDictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["Counters"] = ToCounterDictionary(ReadMember(ReadMember(value, "rngSet"), "Counters")),
                },
            })
            .ToList();

        return new(StringComparer.Ordinal)
        {
            ["Creatures"] = creatures,
            ["Players"] = players,
            ["Rng"] = new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["Seed"] = ReadMember(ReadMember(netState, "Rng"), "Seed")?.ToString(),
                ["Counters"] = ToCounterDictionary(ReadMember(ReadMember(netState, "Rng"), "Counters")),
            },
        };
    }

    private static SortedDictionary<string, object?> BuildPlainPile(
        object pile, Dictionary<int, List<object?>> livePiles)
    {
        var pileType = Convert.ToInt32(ReadMember(pile, "pileType") ?? 0);
        var cards = Enumerate(ReadMember(pile, "cards")).Where(card => card != null).ToList();
        if (!livePiles.TryGetValue(pileType, out var liveCards) || liveCards.Count != cards.Count)
            throw new InvalidOperationException($"Cost capture pile mismatch: {pileType}");
        return new(StringComparer.Ordinal)
        {
            ["pileType"] = pileType,
            ["cards"] = cards.Select((card, index) =>
            {
                var saved = ReadMember(card, "card")
                    ?? throw new InvalidOperationException("Net card state is missing card");
                var savedId = ReadMember(ReadMember(saved, "id") ?? ReadMember(saved, "Id"), "Entry")?.ToString();
                var liveId = ReadMember(ReadMember(liveCards[index], "Id"), "Entry")?.ToString();
                if (savedId != liveId)
                    throw new InvalidOperationException($"Cost capture card mismatch: {pileType}/{index}: {savedId} != {liveId}");
                return new SortedDictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["card"] = BuildPlainSerializableCard(saved),
                    ["affliction"] = PlainModelId(ReadMember(card, "affliction")),
                    ["afflictionCount"] = Convert.ToInt32(ReadMember(card, "afflictionCount") ?? 0),
                    ["energyCost"] = BuildPlainEnergyCost(ReadMember(card, "energyCost")),
                    ["runtimeEnergyCost"] = BuildRuntimeEnergyCost(liveCards[index]!),
                    ["keywords"] = Enumerate(ReadMember(card, "keywords"))
                        .Where(keyword => keyword != null).Select(keyword => keyword!.ToString()).ToList(),
                };
            }).ToList(),
        };
    }

    private static SortedDictionary<string, object?> BuildPlainSerializableCard(object card) =>
        new(StringComparer.Ordinal)
        {
            ["id"] = PlainModelId(ReadMember(card, "id") ?? ReadMember(card, "Id"))
                ?? new SortedDictionary<string, object?> { ["Category"] = "CARD", ["Entry"] = "" },
            ["floor_added_to_deck"] = Convert.ToInt32(
                ReadMember(card, "floor_added_to_deck") ?? ReadMember(card, "FloorAddedToDeck") ?? 0),
            ["CurrentUpgradeLevel"] = NullableInt(ReadMember(card, "CurrentUpgradeLevel")),
            ["enchantment"] = BuildPlainSerializableEnchantment(
                ReadMember(card, "enchantment") ?? ReadMember(card, "Enchantment")),
        };

    private static SortedDictionary<string, object?>? BuildPlainSerializableEnchantment(object? enchantment)
    {
        if (enchantment == null)
            return null;
        var id = PlainModelId(ReadMember(enchantment, "id") ?? ReadMember(enchantment, "Id"));
        if (id == null || string.IsNullOrWhiteSpace(id.GetValueOrDefault("Entry")?.ToString()))
            return null;
        return new(StringComparer.Ordinal)
        {
            ["id"] = id,
            ["amount"] = Convert.ToInt32(ReadMember(enchantment, "amount") ?? ReadMember(enchantment, "Amount") ?? 0),
            ["props"] = ReadMember(enchantment, "props") ?? ReadMember(enchantment, "Props"),
        };
    }

    private static SortedDictionary<string, object?>? BuildPlainEnergyCost(object? cost)
    {
        if (cost == null)
            return null;
        // NetFullCombatState.CardState.energyCost is Nullable<int>, not a
        // CardEnergyCost object. Preserve the network value instead of turning
        // every non-null value into {Value:null, ResolvedValue:null}.
        if (cost is IConvertible)
            return new(StringComparer.Ordinal) { ["ResolvedValue"] = Convert.ToInt32(cost) };
        return new(StringComparer.Ordinal)
        {
            ["Value"] = NullableInt(ReadMember(cost, "Value")),
            ["ResolvedValue"] = NullableInt(ReadMember(cost, "ResolvedValue")),
        };
    }

    private static SortedDictionary<string, object?> BuildRuntimeEnergyCost(object card)
    {
        var cost = ReadMember(card, "EnergyCost")
            ?? throw new InvalidOperationException("Runtime card has no energy cost");
        return new(StringComparer.Ordinal)
        {
            ["Base"] = Convert.ToInt32(ReadMember(cost, "_base")
                ?? throw new InvalidOperationException("Card cost base is unavailable")),
            ["Canonical"] = Convert.ToInt32(ReadMember(cost, "Canonical")
                ?? throw new InvalidOperationException("Card canonical cost is unavailable")),
            ["CostsX"] = Convert.ToBoolean(ReadMember(cost, "CostsX") ?? false),
            ["CapturedXValue"] = Convert.ToInt32(ReadMember(cost, "_capturedXValue") ?? 0),
            ["WasJustUpgraded"] = Convert.ToBoolean(ReadMember(cost, "WasJustUpgraded") ?? false),
            ["LocalModifiers"] = Enumerate(ReadMember(cost, "_localModifiers"))
                .Where(modifier => modifier != null)
                .Select(modifier => new SortedDictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["Amount"] = Convert.ToInt32(ReadMember(modifier, "Amount") ?? 0),
                    ["Type"] = Convert.ToInt32(ReadMember(modifier, "Type") ?? 0),
                    ["Expiration"] = Convert.ToInt32(ReadMember(modifier, "Expiration") ?? 0),
                    ["IsReduceOnly"] = Convert.ToBoolean(ReadMember(modifier, "IsReduceOnly") ?? false),
                }).ToList(),
        };
    }

    private static List<SortedDictionary<string, object?>> CaptureDetailedRngStates(object? rngSet)
    {
        var result = new List<SortedDictionary<string, object?>>();
        if (ReadMember(rngSet, "_rngs") is not IDictionary dictionary)
            return result;
        foreach (DictionaryEntry entry in dictionary)
        {
            var name = entry.Key?.ToString();
            if (string.IsNullOrWhiteSpace(name) || entry.Value == null)
                continue;
            var rng = entry.Value;
            var random = ReadMember(rng, "_random");
            var implementation = ReadMember(random, "_impl");
            var prng = ReadMember(implementation, "_prng");
            result.Add(new(StringComparer.Ordinal)
            {
                ["Name"] = name,
                ["Counter"] = Convert.ToInt32(ReadMember(rng, "<Counter>k__BackingField") ?? 0),
                ["Seed"] = NullableUInt32(ReadMember(rng, "<Seed>k__BackingField")),
                ["S0"] = NullableUInt64(ReadMember(random, "_s0")),
                ["S1"] = NullableUInt64(ReadMember(random, "_s1")),
                ["S2"] = NullableUInt64(ReadMember(random, "_s2")),
                ["S3"] = NullableUInt64(ReadMember(random, "_s3")),
                ["Inext"] = NullableInt(ReadMember(prng, "_inext")),
                ["Inextp"] = NullableInt(ReadMember(prng, "_inextp")),
                ["SeedArray"] = ReadMember(prng, "_seedArray") is int[] values ? values.ToList() : null,
            });
        }
        return result.OrderBy(row => row["Name"]?.ToString(), StringComparer.Ordinal).ToList();
    }

    private static List<SortedDictionary<string, object?>> CaptureEnemyCreatureStates(IReadOnlyList<Creature> enemies) =>
        enemies.Where(enemy => enemy != null && enemy.IsAlive)
            .Select(enemy => new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["MonsterId"] = enemy.Monster?.Id.Entry ?? enemy.Name ?? "UNKNOWN",
                ["CurrentHp"] = enemy.CurrentHp,
                ["MaxHp"] = enemy.MaxHp,
                ["Block"] = enemy.Block,
                ["SpawnedThisTurn"] = ReadMember(enemy.Monster, "_spawnedThisTurn") as bool?,
                ["Powers"] = enemy.Powers.Select(power => new SortedDictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["Id"] = power.Id.Entry,
                    ["Amount"] = power.Amount,
                    ["AmountOnTurnStart"] = ReadMember(power, "_amountOnTurnStart") as int?,
                }).ToList(),
            }).ToList();

    private static List<SortedDictionary<string, object?>> CaptureEnemyAiStates(IReadOnlyList<Creature> enemies)
    {
        var result = new List<SortedDictionary<string, object?>>();
        foreach (var enemy in enemies)
        {
            var monster = enemy.Monster;
            var machine = monster?.MoveStateMachine;
            var states = ReadMember(machine, "States") as IDictionary;
            var reverse = new Dictionary<object, string>(ReferenceEqualityComparer.Instance);
            if (states != null)
            {
                foreach (DictionaryEntry entry in states)
                {
                    if (entry.Value != null)
                        reverse[entry.Value] = entry.Key?.ToString() ?? "";
                }
            }
            string? StateId(object? value)
            {
                if (value == null) return null;
                if (reverse.TryGetValue(value, out var id) && !string.IsNullOrWhiteSpace(id)) return id;
                return ReadMember(value, "Id")?.ToString() ?? ReadMember(value, "StateId")?.ToString();
            }
            var stateLog = Enumerate(ReadMember(machine, "StateLog"))
                .Select(StateId).Where(id => !string.IsNullOrWhiteSpace(id)).Cast<string>().ToList();
            result.Add(new(StringComparer.Ordinal)
            {
                ["MonsterId"] = monster?.Id.Entry ?? "",
                ["CurrentStateId"] = StateId(ReadMember(machine, "_currentState") ?? ReadMember(machine, "CurrentState")),
                ["InitialStateId"] = StateId(ReadMember(machine, "_initialState") ?? ReadMember(machine, "InitialState")),
                ["NextMoveId"] = StateId(ReadMember(monster, "NextMove")),
                ["PerformedFirstMove"] = ReadMember(machine, "_performedFirstMove") as bool?,
                ["StateLogIds"] = stateLog,
            });
        }
        return result;
    }

    private static List<SortedDictionary<string, object?>> CaptureRelicStates(Player player)
    {
        var result = new List<SortedDictionary<string, object?>>();
        foreach (var relic in player.Relics ?? Enumerable.Empty<RelicModel>())
        {
            if (relic == null) continue;
            var fields = new SortedDictionary<string, object?>(StringComparer.Ordinal);
            var nullReferences = new List<string>();
            foreach (var field in relic.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
            {
                if (field.IsInitOnly) continue;
                object? value;
                try { value = field.GetValue(relic); } catch { continue; }
                var type = field.FieldType;
                if (type.IsPrimitive || type.IsEnum || type == typeof(string) || type == typeof(decimal))
                    fields[field.Name] = value is Enum ? value.ToString() : value;
                else if (!type.IsValueType && value == null)
                    nullReferences.Add(field.Name);
            }
            if (fields.Count > 0 || nullReferences.Count > 0)
            {
                result.Add(new(StringComparer.Ordinal)
                {
                    ["Id"] = relic.Id.Entry,
                    ["Fields"] = fields,
                    ["NullRefFields"] = nullReferences.Count > 0 ? nullReferences : null,
                });
            }
        }
        return result;
    }

    private static SortedDictionary<string, object?>? PlainModelId(object? modelId)
    {
        if (modelId == null) return null;
        return new(StringComparer.Ordinal)
        {
            ["Category"] = ReadMember(modelId, "Category")?.ToString(),
            ["Entry"] = ReadMember(modelId, "Entry")?.ToString(),
        };
    }

    private static SortedDictionary<string, int> ToCounterDictionary(object? counters)
    {
        var result = new SortedDictionary<string, int>(StringComparer.Ordinal);
        if (counters is IDictionary dictionary)
        {
            foreach (DictionaryEntry entry in dictionary)
            {
                if (entry.Key != null)
                    result[entry.Key.ToString()!] = Convert.ToInt32(entry.Value ?? 0);
            }
            return result;
        }
        foreach (var item in Enumerate(counters))
        {
            var key = ReadMember(item, "Key");
            if (key != null)
                result[key.ToString()!] = Convert.ToInt32(ReadMember(item, "Value") ?? 0);
        }
        return result;
    }

    private static IEnumerable<object?> Enumerate(object? value) =>
        value is IEnumerable enumerable ? enumerable.Cast<object?>() : Enumerable.Empty<object?>();

    private static object? ReadMember(object? value, string name)
    {
        if (value == null) return null;
        for (var type = value.GetType(); type != null; type = type.BaseType)
        {
            var property = type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public |
                BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (property != null) return property.GetValue(value);
            var field = type.GetField(name, BindingFlags.Instance | BindingFlags.Public |
                BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (field != null) return field.GetValue(value);
            var backing = type.GetField($"<{name}>k__BackingField",
                BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (backing != null) return backing.GetValue(value);
        }
        return null;
    }

    private static int? NullableInt(object? value) => value == null ? null : Convert.ToInt32(value);
    private static uint? NullableUInt32(object? value) => value == null ? null : Convert.ToUInt32(value);
    private static ulong? NullableUInt64(object? value) => value == null ? null : Convert.ToUInt64(value);

    private sealed class ReferenceEqualityComparer : IEqualityComparer<object>
    {
        public static readonly ReferenceEqualityComparer Instance = new();
        public new bool Equals(object? x, object? y) => ReferenceEquals(x, y);
        public int GetHashCode(object value) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(value);
    }
}
