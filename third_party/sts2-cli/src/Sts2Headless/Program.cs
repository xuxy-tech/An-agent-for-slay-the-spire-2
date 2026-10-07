using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Sts2Headless;

class Program
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
    };

    /// <summary>
    /// Locate the directory containing sts2.dll: STS2_LIB env, walk up from BaseDirectory, then BaseDirectory/lib.
    /// </summary>
    private static string ResolveLibDirectory()
    {
        var envLib = Environment.GetEnvironmentVariable("STS2_LIB");
        if (!string.IsNullOrWhiteSpace(envLib))
        {
            var p = Path.GetFullPath(envLib.Trim());
            if (Directory.Exists(p) && File.Exists(Path.Combine(p, "sts2.dll")))
                return p;
        }

        var dir = AppContext.BaseDirectory;
        for (var depth = 0; depth < 16 && !string.IsNullOrEmpty(dir); depth++)
        {
            var candidate = Path.Combine(dir, "lib");
            if (Directory.Exists(candidate) && File.Exists(Path.Combine(candidate, "sts2.dll")))
                return Path.GetFullPath(candidate);
            dir = Directory.GetParent(dir)?.FullName ?? "";
        }

        return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "lib"));
    }

    static void Main(string[] args)
    {
        Console.InputEncoding = new System.Text.UTF8Encoding(false, true);
        Console.OutputEncoding = new System.Text.UTF8Encoding(false, true);
        // Prevent unhandled exceptions from crashing the process
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
        {
            Console.Error.WriteLine($"[FATAL] Unhandled: {e.ExceptionObject}");
        };
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Console.Error.WriteLine($"[WARN] Unobserved task exception: {e.Exception?.Message}");
            e.SetObserved();
        };

        var libDir = ResolveLibDirectory();

        AssemblyLoadContext.Default.Resolving += (ctx, name) =>
        {
            var path = Path.Combine(libDir, name.Name + ".dll");
            if (File.Exists(path))
                return ctx.LoadFromAssemblyPath(Path.GetFullPath(path));

            // Also check game directory (via STS2_GAME_DIR env var)
            var gameDir = Environment.GetEnvironmentVariable("STS2_GAME_DIR") ?? "";
            if (!string.IsNullOrEmpty(gameDir))
            {
                path = Path.Combine(gameDir, name.Name + ".dll");
                if (File.Exists(path))
                    return ctx.LoadFromAssemblyPath(path);
            }

            return null;
        };

        var sim = new RunSimulator();
        WriteLine(new Dictionary<string, object?> { ["type"] = "ready", ["version"] = "0.2.0",
            ["protocol_capabilities"] = new[] { "ack_event_reward", "exact_save_json", "native_reward_items_v1" } });

        string? line;
        while ((line = Console.ReadLine()) != null)
        {
            line = line.Trim();
            if (string.IsNullOrEmpty(line)) continue;

            Dictionary<string, object?>? result;
            try
            {
                var cmd = JsonSerializer.Deserialize<JsonElement>(line);
                result = HandleCommand(sim, cmd);
            }
            catch (JsonException ex)
            {
                result = new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Invalid JSON: {ex.Message}" };
            }
            catch (Exception ex)
            {
                result = new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"{ex.GetType().Name}: {ex.Message}" };
            }

            if (result != null)
            {
                WriteLine(result);
                if (result.TryGetValue("type", out var resultTypeObj) &&
                    string.Equals(resultTypeObj as string, "quit_result", StringComparison.Ordinal))
                {
                    break;
                }
            }
        }
    }

    static Dictionary<string, object?>? HandleCommand(RunSimulator sim, JsonElement cmd)
    {
        var cmdType = cmd.GetProperty("cmd").GetString() ?? "";
        switch (cmdType)
        {
            case "start_run":
                var unlockMode = cmd.TryGetProperty("unlock_mode", out var um)
                    ? um.GetString() ?? "all"
                    : "all";
                string? progressJson = cmd.TryGetProperty("progress_json", out var pj)
                    ? pj.GetString()
                    : null;
                if (progressJson == null && cmd.TryGetProperty("progress_path", out var pp))
                {
                    var progressPath = pp.GetString();
                    if (!string.IsNullOrWhiteSpace(progressPath))
                    {
                        if (!File.Exists(progressPath))
                            return new Dictionary<string, object?>
                            {
                                ["type"] = "error",
                                ["message"] = $"Progress file not found: {progressPath}",
                            };
                        progressJson = File.ReadAllText(progressPath);
                    }
                }
                return sim.StartRun(
                    cmd.TryGetProperty("character", out var ch) ? ch.GetString() ?? "Ironclad" : "Ironclad",
                    cmd.TryGetProperty("ascension", out var asc) ? asc.GetInt32() : 0,
                    cmd.TryGetProperty("seed", out var s) ? s.GetString() : null,
                    cmd.TryGetProperty("lang", out var lang) ? lang.GetString() ?? "en" : "en",
                    unlockMode,
                    progressJson
                );

            case "start_test_combat":
                return sim.StartTestCombat(
                    cmd.TryGetProperty("character", out var tch) ? tch.GetString() ?? "Ironclad" : "Ironclad",
                    cmd.TryGetProperty("encounter", out var ten) ? ten.GetString() ?? "SHRINKER_BEETLE_WEAK" : "SHRINKER_BEETLE_WEAK",
                    cmd.TryGetProperty("ascension", out var tasc) ? tasc.GetInt32() : 0,
                    cmd.TryGetProperty("seed", out var ts) ? ts.GetString() : null,
                    cmd.TryGetProperty("lang", out var tlang) ? tlang.GetString() ?? "en" : "en"
                );

            case "action":
            {
                var action = cmd.GetProperty("action").GetString() ?? "";
                Dictionary<string, object?>? actionArgs = null;
                if (cmd.TryGetProperty("args", out var argsElem))
                {
                    actionArgs = new Dictionary<string, object?>();
                    foreach (var prop in argsElem.EnumerateObject())
                    {
                        actionArgs[prop.Name] = prop.Value.ValueKind switch
                        {
                            JsonValueKind.Number => prop.Value.GetInt32(),
                            JsonValueKind.String => prop.Value.GetString(),
                            JsonValueKind.True => true,
                            JsonValueKind.False => false,
                            _ => prop.Value.ToString(),
                        };
                    }
                }
                var actionStarted = System.Diagnostics.Stopwatch.GetTimestamp();
                sim.BeginActionExecutionProfile();
                var actionResult = sim.ExecuteAction(action, actionArgs);
                sim.AttachActionExecutionProfile(actionResult);
                actionResult["headless_execute_ms"] =
                    System.Diagnostics.Stopwatch.GetElapsedTime(actionStarted).TotalMilliseconds;
                var compact = cmd.TryGetProperty("compact", out var compactAction) &&
                    compactAction.ValueKind == JsonValueKind.True;
                return compact ? CompactSearchActionResult(actionResult) : actionResult;
            }
            case "action_with_engine_snapshot":
            {
                var action = cmd.GetProperty("action").GetString() ?? "";
                Dictionary<string, object?>? actionArgs = null;
                if (cmd.TryGetProperty("args", out var argsElem))
                {
                    actionArgs = new Dictionary<string, object?>();
                    foreach (var prop in argsElem.EnumerateObject())
                    {
                        actionArgs[prop.Name] = prop.Value.ValueKind switch
                        {
                            JsonValueKind.Number => prop.Value.GetInt32(),
                            JsonValueKind.String => prop.Value.GetString(),
                            JsonValueKind.True => true,
                            JsonValueKind.False => false,
                            _ => prop.Value.ToString(),
                        };
                    }
                }
                return sim.ExecuteActionWithEngineSnapshot(action, actionArgs);
            }

            case "load_save":
            {
                var savePath = cmd.TryGetProperty("path", out var sp) ? sp.GetString() : null;
                var saveJson = cmd.TryGetProperty("json", out var sj) ? sj.GetString() : null;
                if (saveJson == null && savePath != null)
                {
                    if (!File.Exists(savePath))
                        return new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Save file not found: {savePath}" };
                    saveJson = File.ReadAllText(savePath);
                }
                if (saveJson == null)
                    return new Dictionary<string, object?> { ["type"] = "error", ["message"] = "Provide 'path' or 'json' for load_save" };
                var loadLang = cmd.TryGetProperty("lang", out var le) ? (le.GetString() ?? "en") : "en";
                var resumeLatestRoom = cmd.TryGetProperty("resume_room", out var rr) && rr.GetBoolean();
                return sim.LoadSave(saveJson, loadLang, resumeLatestRoom);
            }
            case "get_map":
                return sim.GetFullMap();

            case "set_player":
            {
                var args = new Dictionary<string, JsonElement>();
                foreach (var prop in cmd.EnumerateObject())
                    if (prop.Name != "cmd") args[prop.Name] = prop.Value;
                return sim.SetPlayer(args);
            }

            case "configure_sandbox":
                return sim.ConfigureSandbox(cmd);

            case "enter_room":
            {
                var roomType = cmd.TryGetProperty("type", out var rt) ? rt.GetString() ?? "" : "";
                var encounter = cmd.TryGetProperty("encounter", out var enc) ? enc.GetString() : null;
                var eventId = cmd.TryGetProperty("event", out var ev) ? ev.GetString() : null;
                return sim.EnterRoom(roomType, encounter, eventId);
            }

            case "set_draw_order":
            {
                var cards = new List<string>();
                if (cmd.TryGetProperty("cards", out var cardsArr))
                    foreach (var c in cardsArr.EnumerateArray())
                        cards.Add(c.GetString() ?? "");
                return sim.SetDrawOrder(cards);
            }

            case "write_continue_save":
            {
                var outputPath = cmd.TryGetProperty("path", out var op) ? op.GetString() : null;
                return sim.SaveCheckpoint(outputPath);
            }
            case "write_exact_save":
            {
                var outputPath = cmd.TryGetProperty("path", out var op) ? op.GetString() : null;
                return sim.SaveExactState(outputPath);
            }
            case "capture_engine_combat_snapshot":
                return sim.CaptureEngineCombatSnapshot();

            case "get_search_state":
            {
                var stateStarted = System.Diagnostics.Stopwatch.GetTimestamp();
                var stateResult = sim.GetCurrentSearchState();
                stateResult["headless_build_state_ms"] =
                    System.Diagnostics.Stopwatch.GetElapsedTime(stateStarted).TotalMilliseconds;
                return stateResult;
            }

            case "capture_combat_snapshot":
            {
                var snapshotId = cmd.TryGetProperty("snapshot_id", out var sid) ? sid.GetString() : null;
                var fingerprintMode = cmd.TryGetProperty("fingerprint_mode", out var captureFingerprintMode)
                    ? captureFingerprintMode.GetString() ?? "all"
                    : "all";
                var captureStarted = System.Diagnostics.Stopwatch.GetTimestamp();
                var captureResult = sim.CaptureCombatSnapshot(snapshotId, fingerprintMode);
                captureResult["headless_capture_ms"] =
                    System.Diagnostics.Stopwatch.GetElapsedTime(captureStarted).TotalMilliseconds;
                return captureResult;
            }

            case "fingerprint_combat_snapshot":
            {
                var snapshotId = cmd.TryGetProperty("snapshot_id", out var sid) ? sid.GetString() : null;
                if (string.IsNullOrWhiteSpace(snapshotId))
                    return new Dictionary<string, object?>
                    {
                        ["type"] = "error",
                        ["message"] = "Provide 'snapshot_id' for fingerprint_combat_snapshot",
                    };
                var fingerprintMode = cmd.TryGetProperty("fingerprint_mode", out var fingerprintModeElem)
                    ? fingerprintModeElem.GetString() ?? "all"
                    : "all";
                var fingerprintStarted = System.Diagnostics.Stopwatch.GetTimestamp();
                var fingerprintResult = sim.FingerprintCombatSnapshot(
                    snapshotId!, fingerprintMode);
                fingerprintResult["headless_fingerprint_ms"] =
                    System.Diagnostics.Stopwatch.GetElapsedTime(fingerprintStarted).TotalMilliseconds;
                return fingerprintResult;
            }

            case "expand_combat_children":
            {
                var parentSnapshotId = cmd.TryGetProperty("parent_snapshot_id", out var parentSid)
                    ? parentSid.GetString()
                    : null;
                if (string.IsNullOrWhiteSpace(parentSnapshotId))
                    return new Dictionary<string, object?>
                    {
                        ["type"] = "error",
                        ["message"] = "Provide 'parent_snapshot_id' for expand_combat_children",
                    };
                if (!cmd.TryGetProperty("children", out var childrenElem) ||
                    childrenElem.ValueKind != JsonValueKind.Array)
                    return new Dictionary<string, object?>
                    {
                        ["type"] = "error",
                        ["message"] = "Provide a 'children' array for expand_combat_children",
                    };

                var children = new List<RunSimulator.CombatChildExpansionRequest>();
                foreach (var child in childrenElem.EnumerateArray())
                {
                    var action = child.TryGetProperty("action", out var childAction)
                        ? childAction.GetString() ?? ""
                        : "";
                    var childSnapshotId = child.TryGetProperty("snapshot_id", out var childSid)
                        ? childSid.GetString() ?? ""
                        : "";
                    if (string.IsNullOrWhiteSpace(action) || string.IsNullOrWhiteSpace(childSnapshotId))
                        return new Dictionary<string, object?>
                        {
                            ["type"] = "error",
                            ["message"] = "Each child requires non-empty 'action' and 'snapshot_id'",
                        };
                    children.Add(new RunSimulator.CombatChildExpansionRequest
                    {
                        Action = action,
                        Args = child.TryGetProperty("args", out var childArgs)
                            ? ParseActionArgs(childArgs)
                            : null,
                        SnapshotId = childSnapshotId,
                    });
                }
                var batchLang = cmd.TryGetProperty("lang", out var batchLangElem)
                    ? batchLangElem.GetString() ?? "en"
                    : "en";
                var fingerprintMode = cmd.TryGetProperty("fingerprint_mode", out var batchFingerprintMode)
                    ? batchFingerprintMode.GetString() ?? "all"
                    : "all";
                return sim.ExpandCombatChildren(
                    parentSnapshotId!, children, batchLang, fingerprintMode);
            }

            case "restore_combat_snapshot":
            {
                var snapshotId = cmd.TryGetProperty("snapshot_id", out var sid) ? sid.GetString() : null;
                if (string.IsNullOrWhiteSpace(snapshotId))
                    return new Dictionary<string, object?> { ["type"] = "error", ["message"] = "Provide 'snapshot_id' for restore_combat_snapshot" };
                var restoreLang = cmd.TryGetProperty("lang", out var rl) ? (rl.GetString() ?? "en") : "en";
                var allowFull = cmd.TryGetProperty("allow_full", out var af) ? af.GetBoolean() : true;
                var compact = cmd.TryGetProperty("compact", out var compactRestore) &&
                    compactRestore.ValueKind == JsonValueKind.True;
                var restoreResult = sim.RestoreCombatSnapshot(snapshotId!, restoreLang, allowFull);
                return compact ? CompactRestoreResult(restoreResult) : restoreResult;
            }
            case "export_combat_snapshot":
            {
                var snapshotId = cmd.TryGetProperty("snapshot_id", out var sid) ? sid.GetString() : null;
                if (string.IsNullOrWhiteSpace(snapshotId))
                    return new Dictionary<string, object?> { ["type"] = "error", ["message"] = "Provide 'snapshot_id' for export_combat_snapshot" };
                return sim.ExportCombatSnapshot(snapshotId!);
            }
            case "import_combat_snapshot":
            {
                var snapshotJson = cmd.TryGetProperty("snapshot_json", out var sj) ? sj.GetString() : null;
                var snapshotId = cmd.TryGetProperty("snapshot_id", out var sid) ? sid.GetString() : null;
                if (string.IsNullOrWhiteSpace(snapshotJson))
                    return new Dictionary<string, object?> { ["type"] = "error", ["message"] = "Provide 'snapshot_json' for import_combat_snapshot" };
                return sim.ImportCombatSnapshot(snapshotJson!, snapshotId);
            }

            case "inspect_enemy_ai":
                return sim.InspectEnemyAi();

            case "inspect_combat_membership":
                return sim.InspectCombatMembership();

            case "inspect_power_methods":
                return sim.InspectPowerMethods();

            case "get_rng_snapshot":
                return sim.GetRngSnapshot();

            case "inspect_rng_state":
                return sim.InspectRngState();

            case "reseed_rng_stream":
            {
                var streamSeeds = new Dictionary<string, int>();
                if (cmd.TryGetProperty("streams", out var streamsEl) &&
                    streamsEl.ValueKind == System.Text.Json.JsonValueKind.Object)
                {
                    foreach (var prop in streamsEl.EnumerateObject())
                        streamSeeds[prop.Name] = prop.Value.GetInt32();
                }
                return sim.ReseedRngStream(streamSeeds);
            }

            case "inspect_rng_graph":
            {
                var rngName = cmd.TryGetProperty("rng_name", out var rn) ? rn.GetString() : "MonsterAi";
                var depth = cmd.TryGetProperty("depth", out var rd) ? rd.GetInt32() : 4;
                return sim.InspectRngGraph(rngName ?? "MonsterAi", depth);
            }

            case "inspect_encounter_state":
                return sim.InspectEncounterState();
            case "inspect_type_methods":
            {
                var typeName = cmd.TryGetProperty("type_name", out var tn) ? tn.GetString() ?? "" : "";
                return sim.InspectTypeMethods(typeName);
            }
            case "inspect_type_shape":
            {
                var typeName = cmd.TryGetProperty("type_name", out var tn) ? tn.GetString() ?? "" : "";
                return sim.InspectTypeShape(typeName);
            }
            case "inspect_relic_picking_state":
                return sim.InspectRelicPickingState();

            case "inspect_power_state":
                return sim.InspectPowerState();

            case "inspect_relic_state":
                return sim.InspectRelicState();

            case "inspect_hook_listeners":
                return sim.InspectHookListeners();

            case "inspect_enemy_graph":
            {
                var enemyIndex = cmd.TryGetProperty("enemy_index", out var ei) ? ei.GetInt32() : 0;
                var depth = cmd.TryGetProperty("depth", out var dd) ? dd.GetInt32() : 3;
                return sim.InspectEnemyGraph(enemyIndex, depth);
            }

            case "inspect_card_cost_runtime":
                return sim.InspectCardCostRuntime();

            case "inspect_all_card_runtime":
                return sim.InspectAllCardRuntime();

            case "inspect_cards":
            {
                var cardIds = new List<string>();
                if (cmd.TryGetProperty("card_ids", out var cardIdsEl) &&
                    cardIdsEl.ValueKind == JsonValueKind.Array)
                {
                    foreach (var cardId in cardIdsEl.EnumerateArray())
                    {
                        var value = cardId.GetString();
                        if (!string.IsNullOrWhiteSpace(value)) cardIds.Add(value);
                    }
                }
                return sim.InspectCards(cardIds);
            }

            case "inspect_combat_runtime":
            {
                var depth = cmd.TryGetProperty("depth", out var crd) ? crd.GetInt32() : 4;
                return sim.InspectCombatRuntime(depth);
            }

            case "inspect_combat_history":
                return sim.InspectCombatHistory();

            case "inspect_static_counters":
            {
                var filter = cmd.TryGetProperty("filter", out var scf) ? scf.GetString() : null;
                return sim.InspectStaticCounters(filter);
            }

            case "dedup_pile_handlers":
                return sim.DedupPileHandlers();

            case "damage_trace":
            {
                var op = cmd.TryGetProperty("op", out var dto) ? dto.GetString() : "read";
                if (op == "on") { RunSimulator.DamageTrace.Enabled = true; RunSimulator.DamageTrace.Log.Clear(); }
                else if (op == "off") { RunSimulator.DamageTrace.Enabled = false; }
                else if (op == "clear") { RunSimulator.DamageTrace.Log.Clear(); }
                return new Dictionary<string, object?>
                {
                    ["success"] = true,
                    ["enabled"] = RunSimulator.DamageTrace.Enabled,
                    ["log"] = new List<string>(RunSimulator.DamageTrace.Log),
                };
            }

            case "quit":
            {
                var outputPath = cmd.TryGetProperty("path", out var op) ? op.GetString() : null;
                if (!string.IsNullOrEmpty(outputPath))
                {
                    var saveResult = sim.SaveCheckpoint(outputPath);
                    bool saveOk = saveResult.TryGetValue("success", out var sObj) && sObj is bool b && b;
                    if (!saveOk)
                    {
                        // Save failed — do NOT clean up so the caller can retry with a different path.
                        return new Dictionary<string, object?>
                        {
                            ["type"] = "save_error",
                            ["save"] = saveResult,
                        };
                    }
                    sim.CleanUp();
                    return new Dictionary<string, object?>
                    {
                        ["type"] = "quit_result",
                        ["success"] = true,
                        ["save"] = saveResult,
                    };
                }
                sim.CleanUp();
                return new Dictionary<string, object?>
                {
                    ["type"] = "quit_result",
                    ["success"] = true,
                    ["save"] = null,
                };
            }

            default:
                return new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Unknown command: {cmdType}" };
        }
    }

    static Dictionary<string, object?> ParseActionArgs(JsonElement argsElem)
    {
        var actionArgs = new Dictionary<string, object?>();
        if (argsElem.ValueKind != JsonValueKind.Object)
            return actionArgs;
        foreach (var prop in argsElem.EnumerateObject())
        {
            actionArgs[prop.Name] = prop.Value.ValueKind switch
            {
                JsonValueKind.Number when prop.Value.TryGetInt32(out var intValue) => intValue,
                JsonValueKind.Number when prop.Value.TryGetInt64(out var longValue) => longValue,
                JsonValueKind.Number => prop.Value.GetDouble(),
                JsonValueKind.String => prop.Value.GetString(),
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                JsonValueKind.Null => null,
                _ => prop.Value.ToString(),
            };
        }
        return actionArgs;
    }

    static Dictionary<string, object?> CompactSearchActionResult(
        Dictionary<string, object?> result)
    {
        // A normal combat-play action is immediately followed by an explicit
        // get_search_state in the search protocol. Avoid serializing the same
        // player/hand/enemy state twice. Terminal, modal-selection and error
        // results remain complete because the searcher consumes their payload.
        var isError = result.TryGetValue("type", out var type) &&
            string.Equals(type?.ToString(), "error", StringComparison.Ordinal);
        var isCombatPlay = result.TryGetValue("decision", out var decision) &&
            string.Equals(decision?.ToString(), "combat_play", StringComparison.Ordinal);
        // Search terminates at combat victory and never visits the room reward
        // screen. Keep the live action protocol's explicit reward boundary,
        // while presenting that boundary as victory to search workers only.
        if (!isError && string.Equals(decision?.ToString(), "combat_reward", StringComparison.Ordinal))
        {
            var searchTerminal = new Dictionary<string, object?>(result);
            searchTerminal["decision"] = "victory";
            return searchTerminal;
        }
        if (isError || !isCombatPlay)
            return result;

        return SelectFields(
            result,
            "type", "success", "decision", "message", "headless_execute_ms",
            "headless_wait_profile"
        );
    }

    static Dictionary<string, object?> CompactRestoreResult(
        Dictionary<string, object?> result)
    {
        // Search restores never consume the rendered decision state; they only
        // need success/error and restore diagnostics before get_search_state.
        // Preserve complete errors so recovery logs retain their evidence.
        var isError = result.TryGetValue("type", out var type) &&
            string.Equals(type?.ToString(), "error", StringComparison.Ordinal);
        if (isError)
            return result;

        return SelectFields(
            result,
            "type", "success", "decision", "message", "restored_snapshot_id",
            "restore_mode", "restore_timing_ms", "restore_sanity_warning"
        );
    }

    static Dictionary<string, object?> SelectFields(
        Dictionary<string, object?> source,
        params string[] fields)
    {
        var compact = new Dictionary<string, object?> { ["compact"] = true };
        foreach (var field in fields)
            if (source.TryGetValue(field, out var value))
                compact[field] = value;
        return compact;
    }

    static void WriteLine(Dictionary<string, object?> data)
    {
        Console.Out.WriteLine(JsonSerializer.Serialize(data, JsonOpts));
        Console.Out.Flush();
    }
}
