using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;
using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Runs;

namespace STS2HumanCapture;

[ModInitializer("Initialize")]
public static class Entry
{
    private static int _started;

    public static void Initialize()
    {
        if (Interlocked.Exchange(ref _started, 1) != 0)
            return;
        GameThread.Initialize();
        CapturePatches.Initialize();
        PlayerActionEvents.Start();
        CaptureHttpServer.Start();
        Log.Info("[STS2HumanCapture] Ready on http://127.0.0.1:9878/", 2);
    }
}

internal static class GameThread
{
    public static readonly TimeSpan ReadTimeout = TimeSpan.FromSeconds(2);
    private static SynchronizationContext? _context;
    private static int _threadId;

    public static void Initialize()
    {
        _context = SynchronizationContext.Current
            ?? throw new InvalidOperationException("Could not capture the game SynchronizationContext");
        _threadId = Environment.CurrentManagedThreadId;
    }

    public static Task<T> InvokeAsync<T>(Func<T> action, CancellationToken cancellationToken = default)
    {
        if (_context == null)
            throw new InvalidOperationException("Game thread is not initialized");
        if (cancellationToken.IsCancellationRequested)
            return Task.FromCanceled<T>(cancellationToken);
        if (Environment.CurrentManagedThreadId == _threadId)
            return Task.FromResult(action());
        var completion = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        var cancellation = cancellationToken.Register(
            () => completion.TrySetCanceled(cancellationToken));
        _context.Post(_ =>
        {
            try
            {
                if (cancellationToken.IsCancellationRequested)
                {
                    completion.TrySetCanceled(cancellationToken);
                    return;
                }
                completion.TrySetResult(action());
            }
            catch (Exception ex) { completion.TrySetException(ex); }
            finally { cancellation.Dispose(); }
        }, null);
        return completion.Task;
    }

    public static async Task<T> InvokeReadAsync<T>(Func<T> action)
    {
        using var timeout = new CancellationTokenSource(ReadTimeout);
        return await InvokeAsync(action, timeout.Token);
    }
}

internal sealed record CaptureEnvelope(long event_id, string type, string timestamp_utc, object data);

internal static class CapturePatches
{
    private const string Owner = "vesper.sts2.human-capture";
    private static IReadOnlyList<string> _targets = Array.Empty<string>();

    public static IReadOnlyList<string> Targets => _targets;

    public static void Initialize()
    {
        var harmony = new Harmony(Owner);
        harmony.PatchAll(typeof(Entry).Assembly);
        _targets = Harmony.GetAllPatchedMethods()
            .Where(method => Harmony.GetPatchInfo(method)?.Owners.Contains(Owner) == true)
            .Select(method => $"{method.DeclaringType?.FullName}.{method.Name}")
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (_targets.Count == 0)
            throw new InvalidOperationException("Human-capture Harmony patches did not bind");
    }
}

internal static class PlayerActionEvents
{
    private static readonly Channel<CaptureEnvelope> Events = Channel.CreateUnbounded<CaptureEnvelope>(
        new UnboundedChannelOptions
        {
            SingleReader = true,
            SingleWriter = false,
        });
    private static ActionQueueSet? _attachedQueue;
    private static long _nextEventId;

    public static ChannelReader<CaptureEnvelope> Reader => Events.Reader;

    public static void Start() => _ = Task.Run(AttachLoopAsync);

    private static async Task AttachLoopAsync()
    {
        while (true)
        {
            try { await GameThread.InvokeAsync(AttachCurrentQueue); }
            catch (Exception ex) { Log.Warn("[STS2HumanCapture] Queue attach failed: " + ex.Message, 2); }
            await Task.Delay(250).ConfigureAwait(false);
        }
    }

    private static bool AttachCurrentQueue()
    {
        var current = RunManager.Instance.ActionQueueSet;
        if (ReferenceEquals(current, _attachedQueue))
            return false;
        if (_attachedQueue != null)
            _attachedQueue.ActionEnqueued -= OnActionEnqueued;
        _attachedQueue = current;
        NativeActionLifecycle.Attach(current);
        if (_attachedQueue != null)
            _attachedQueue.ActionEnqueued += OnActionEnqueued;
        Publish("capture_source_changed", new { attached = _attachedQueue != null });
        return true;
    }

    private static void OnActionEnqueued(GameAction action)
    {
        try
        {
            if (!ActionQueueSet.IsGameActionPlayerDriven(action))
                return;
            var netService = RunManager.Instance.NetService;
            if (netService != null && action.OwnerId != netService.NetId)
                return;
            var detail = DescribeAction(action);
            detail["source"] = "combat_action_queue";
            detail["phase"] = "committed";
            detail["authoritative_snapshot"] = AuthoritativeCombatSnapshots.CaptureForEvent();
            Publish("player_action_observed", detail);
        }
        catch (Exception ex)
        {
            Log.Warn("[STS2HumanCapture] Combat action capture failed: " + ex.Message, 2);
        }
    }

    private static SortedDictionary<string, object?> DescribeAction(GameAction action)
    {
        var detail = new SortedDictionary<string, object?>(StringComparer.Ordinal)
        {
            ["action_class"] = action.GetType().FullName,
            ["action_type"] = action.ActionType.ToString(),
            ["owner_id"] = action.OwnerId,
            ["queue_action_id"] = ReadMember(action, "Id"),
            ["display"] = action.ToString(),
        };
        switch (action)
        {
            case PlayCardAction card:
                detail["semantic_action"] = "play_card";
                detail["card_id"] = card.CardModelId.ToString();
                detail["card_instance"] = ScalarMembers(card.NetCombatCard);
                detail["target_creature_id"] = card.TargetId;
                break;
            case UsePotionAction potion:
                detail["semantic_action"] = "use_potion";
                detail["potion_index"] = potion.PotionIndex;
                detail["target_creature_id"] = potion.TargetId;
                detail["target_player_id"] = ReadMember(potion, "TargetPlayerId");
                detail["enqueued_in_combat"] = potion.WasEnqueuedInCombat;
                try
                {
                    var potionModel = potion.Player.GetPotionAtSlotIndex((int)potion.PotionIndex);
                    if (potionModel == null)
                    {
                        detail["target_semantics_source"] = "unavailable";
                        detail["target_semantics_error"] = "empty_potion_slot";
                    }
                    else
                    {
                        var targetType = potionModel.TargetType;
                        var requiresTarget = targetType == TargetType.AnyEnemy;
                        detail["potion_id"] = potionModel.Id.Entry;
                        detail["target_type"] = targetType.ToString();
                        detail["requires_target"] = requiresTarget;
                        detail["target_index_space"] = requiresTarget ? "enemies" : null;
                        detail["target_semantics_source"] = "potion_model";
                    }
                }
                catch (Exception ex)
                {
                    // Preserve the action, but never infer UI targeting from resolved target IDs.
                    detail["target_semantics_source"] = "unavailable";
                    detail["target_semantics_error"] = ex.GetType().Name;
                }
                break;
            case EndPlayerTurnAction:
                detail["semantic_action"] = "end_turn";
                detail["turn_number"] = ReadMember(action, "_turnNumber");
                break;
            case DiscardPotionGameAction discarded:
                detail["semantic_action"] = "discard_potion";
                detail["potion_index"] = ReadMember(discarded, "_potionSlotIndex");
                detail["enqueued_in_combat"] = discarded.WasEnqueuedInCombat;
                break;
            default:
                detail["semantic_action"] = "game_action";
                detail["scalar_members"] = ScalarMembers(action);
                break;
        }
        return detail;
    }

    internal static SortedDictionary<string, object?> ScalarMembers(object? value)
    {
        var result = new SortedDictionary<string, object?>(StringComparer.Ordinal);
        if (value == null)
            return result;
        foreach (var property in value.GetType().GetProperties(BindingFlags.Instance | BindingFlags.Public))
        {
            if (property.GetIndexParameters().Length != 0 || !IsScalar(property.PropertyType))
                continue;
            try { result[property.Name] = property.GetValue(value); }
            catch { }
        }
        return result;
    }

    private static bool IsScalar(Type type)
    {
        type = Nullable.GetUnderlyingType(type) ?? type;
        return type.IsPrimitive || type.IsEnum || type == typeof(string) || type == typeof(Guid)
            || type == typeof(decimal);
    }

    internal static object? ReadMember(object value, string name)
    {
        for (var type = value.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetField(name, BindingFlags.Instance | BindingFlags.Public |
                BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (field != null)
                return field.GetValue(value);
            var property = type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public |
                BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (property != null)
                return property.GetValue(value);
        }
        return null;
    }

    internal static void PublishObserved(
        string source,
        string semanticAction,
        IDictionary<string, object?>? detail = null,
        string phase = "requested")
    {
        var payload = new SortedDictionary<string, object?>(StringComparer.Ordinal)
        {
            ["source"] = source,
            ["phase"] = phase,
            ["semantic_action"] = semanticAction,
            ["owner_id"] = RunManager.Instance.NetService?.NetId,
        };
        if (detail != null)
        {
            foreach (var (key, value) in detail)
                payload[key] = value;
        }
        payload["authoritative_snapshot"] = AuthoritativeCombatSnapshots.CaptureForEvent();
        Publish("player_action_observed", payload);
    }

    private static void Publish(string type, object data)
    {
        Events.Writer.TryWrite(new CaptureEnvelope(
            Interlocked.Increment(ref _nextEventId), type, DateTime.UtcNow.ToString("O"), data));
    }
}

internal static class CaptureHttpServer
{
    private static Task? _recoveryTask;
    private const string ProtocolVersion = "2026-09-20-human-capture-v4";
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
    private static HttpListener? _listener;

    public static void Start()
    {
        _listener = new HttpListener();
        _listener.Prefixes.Add("http://127.0.0.1:9878/");
        _listener.Start();
        _ = Task.Run(ListenLoopAsync);
    }

    private static async Task ListenLoopAsync()
    {
        while (_listener?.IsListening == true)
        {
            HttpListenerContext context;
            try { context = await _listener.GetContextAsync().ConfigureAwait(false); }
            catch when (_listener?.IsListening != true) { return; }
            _ = HandleAsync(context);
        }
    }

    private static async Task HandleAsync(HttpListenerContext context)
    {
        try
        {
            var path = context.Request.Url?.AbsolutePath;
            if (context.Request.HttpMethod == "GET" && path == "/health")
            {
                await WriteJsonAsync(context.Response, new
                {
                    ok = true,
                    data = new
                    {
                        service = "sts2-human-capture",
                        protocol_version = ProtocolVersion,
                        status = "ready",
                        patch_count = CapturePatches.Targets.Count,
                        patch_targets = CapturePatches.Targets,
                        authoritative_combat_snapshot = true,
                        current_combat_snapshot = true,
                        snapshot_schema = AuthoritativeCombatSnapshots.Schema,
                        snapshot_capture_phase = "synchronous_before_action_execution",
                        authoritative_run_save = true,
                        reward_contract = NativeRewards.Contract,
                        recovery_main_menu = true,
                        potion_target_contract = CapturePatches.Targets.Contains(
                            "MegaCrit.Sts2.Core.Models.PotionModel.EnqueueManualUse")
                            ? PotionTargetCompatibility.Contract : null,
                        native_action_lifecycle = "sts2.native_action_lifecycle.v1",
                        crystal_sphere_contract = NativeCrystalSphere.Contract,
                        authoritative_run_save_schema = AuthoritativeRunSaves.Schema,
                    }
                });
                return;
            }
            if (context.Request.HttpMethod == "GET" && path == "/identity")
            {
                await WriteJsonAsync(context.Response, new { ok = true, data = BuildIdentity() });
                return;
            }
            if (context.Request.HttpMethod == "GET" && path == "/reward-state")
            {
                var rewards = await GameThread.InvokeReadAsync(NativeRewards.Capture);
                await WriteJsonAsync(context.Response, new { ok = true, data = rewards });
                return;
            }
            if (context.Request.HttpMethod == "GET" && path == "/crystal-sphere/state")
            {
                var state = await GameThread.InvokeReadAsync(NativeCrystalSphere.Capture);
                await WriteJsonAsync(context.Response, new { ok = true, data = state });
                return;
            }
            if (context.Request.HttpMethod == "POST" && path == "/crystal-sphere/action")
            {
                using var request = await JsonDocument.ParseAsync(context.Request.InputStream);
                var root = request.RootElement;
                var action = root.GetProperty("action").GetString();
                Task operation;
                if (action == "crystal_sphere_divine")
                {
                    var x = root.GetProperty("x").GetInt32();
                    var y = root.GetProperty("y").GetInt32();
                    var tool = root.GetProperty("tool").GetString() ?? "";
                    var expected = root.GetProperty("expected_remaining").GetInt32();
                    operation = await GameThread.InvokeAsync(
                        () => NativeCrystalSphere.Click(x, y, tool, expected));
                }
                else if (action == "proceed")
                    operation = await GameThread.InvokeAsync(NativeCrystalSphere.Proceed);
                else
                    throw new ArgumentException("Unsupported Crystal Sphere action");
                await operation;
                await WriteJsonAsync(context.Response, new { ok = true,
                    data = new { action, status = "submitted" } });
                return;
            }
            if (context.Request.HttpMethod == "GET" && path == "/action-lifecycle")
            {
                var lifecycle = await GameThread.InvokeReadAsync(NativeActionLifecycle.Capture);
                await WriteJsonAsync(context.Response, new { ok = true, data = lifecycle });
                return;
            }
            if (context.Request.HttpMethod == "GET" && path == "/events/stream")
            {
                await StreamAsync(context.Response);
                return;
            }
            if (context.Request.HttpMethod == "GET" && path == "/combat-snapshot/current")
            {
                var metadata = await GameThread.InvokeReadAsync(AuthoritativeCombatSnapshots.CaptureForEvent);
                if (!string.Equals(metadata.GetValueOrDefault("status")?.ToString(), "complete", StringComparison.Ordinal)
                    || metadata.GetValueOrDefault("snapshot_id") is not string snapshotId
                    || !AuthoritativeCombatSnapshots.TryGet(snapshotId, out var currentSnapshot))
                {
                    context.Response.StatusCode = 409;
                    await WriteJsonAsync(context.Response, new { ok = false,
                        error = "Current combat snapshot is unavailable: " + metadata.GetValueOrDefault("error"),
                        error_type = metadata.GetValueOrDefault("error_type") });
                    return;
                }
                await WriteJsonAsync(context.Response, new
                {
                    ok = true,
                    data = new
                    {
                        snapshot_id = currentSnapshot.Id,
                        schema = currentSnapshot.Schema,
                        captured_at_utc = currentSnapshot.CapturedAtUtc,
                        sha256 = currentSnapshot.Sha256,
                        bytes = currentSnapshot.Bytes,
                        snapshot_json = currentSnapshot.Json,
                    }
                });
                return;
            }
            if (context.Request.HttpMethod == "GET" && path?.StartsWith("/snapshots/", StringComparison.Ordinal) == true)
            {
                var snapshotId = Uri.UnescapeDataString(path["/snapshots/".Length..]);
                if (!AuthoritativeCombatSnapshots.TryGet(snapshotId, out var snapshot))
                {
                    context.Response.StatusCode = 404;
                    return;
                }
                await WriteJsonAsync(context.Response, new
                {
                    ok = true,
                    data = new
                    {
                        snapshot_id = snapshot.Id,
                        schema = snapshot.Schema,
                        captured_at_utc = snapshot.CapturedAtUtc,
                        sha256 = snapshot.Sha256,
                        bytes = snapshot.Bytes,
                        snapshot_json = snapshot.Json,
                    }
                });
                return;
            }
            if (context.Request.HttpMethod == "POST" && path == "/recovery/main-menu")
            {
                var operation = await GameThread.InvokeAsync(() =>
                {
                    var game = MegaCrit.Sts2.Core.Nodes.NGame.Instance
                        ?? throw new InvalidOperationException("Game is unavailable");
                    if (_recoveryTask == null || _recoveryTask.IsCompleted)
                        _recoveryTask = game.ReturnToMainMenu();
                    return _recoveryTask;
                });
                await operation;
                await WriteJsonAsync(context.Response, new { ok = true, data = new { status = "returned" } });
                return;
            }
            if (context.Request.HttpMethod == "GET" && path == "/exact-save")
            {
                var save = await GameThread.InvokeReadAsync(AuthoritativeRunSaves.Capture);
                await WriteJsonAsync(context.Response, new { ok = true, data = save });
                return;
            }
            context.Response.StatusCode = 404;
        }
        catch (TimeoutException ex)
        {
            Log.Warn("[STS2HumanCapture] Game-thread read timed out: " + ex.Message, 2);
            try { context.Response.StatusCode = 504; } catch { }
        }
        catch (OperationCanceledException ex)
        {
            Log.Warn("[STS2HumanCapture] Game-thread read cancelled: " + ex.Message, 2);
            try { context.Response.StatusCode = 504; } catch { }
        }
        catch (Exception ex)
        {
            Log.Warn("[STS2HumanCapture] HTTP request failed: " + ex.Message, 2);
            try { context.Response.StatusCode = 500; } catch { }
        }
        finally
        {
            try { context.Response.Close(); } catch { }
        }
    }

    private static object BuildIdentity()
    {
        static object Describe(Assembly assembly)
        {
            var path = assembly.Location;
            return new
            {
                name = assembly.GetName().Name,
                version = assembly.GetName().Version?.ToString(),
                sha256 = File.Exists(path)
                    ? Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant()
                    : null,
            };
        }
        return new
        {
            protocol_version = ProtocolVersion,
            patch_count = CapturePatches.Targets.Count,
            patch_targets = CapturePatches.Targets,
            authoritative_combat_snapshot = true,
            current_combat_snapshot = true,
            snapshot_schema = AuthoritativeCombatSnapshots.Schema,
            authoritative_run_save = true,
            authoritative_run_save_schema = AuthoritativeRunSaves.Schema,
            game_assembly = Describe(typeof(RunManager).Assembly),
            capture_assembly = Describe(typeof(Entry).Assembly),
        };
    }

    private static async Task StreamAsync(HttpListenerResponse response)
    {
        response.StatusCode = 200;
        response.ContentType = "text/event-stream";
        response.SendChunked = true;
        response.Headers["Cache-Control"] = "no-cache";
        await WriteRawAsync(response, ": stream opened\n\n");
        while (await PlayerActionEvents.Reader.WaitToReadAsync())
        {
            while (PlayerActionEvents.Reader.TryRead(out var item))
            {
                await WriteRawAsync(response, $"id: {item.event_id}\n");
                await WriteRawAsync(response, $"event: {item.type}\n");
                await WriteRawAsync(response, "data: " + JsonSerializer.Serialize(item, JsonOptions) + "\n\n");
                await response.OutputStream.FlushAsync();
            }
        }
    }

    private static async Task WriteJsonAsync(HttpListenerResponse response, object payload)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(payload, JsonOptions);
        response.StatusCode = 200;
        response.ContentType = "application/json; charset=utf-8";
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes);
    }

    private static ValueTask WriteRawAsync(HttpListenerResponse response, string text) =>
        response.OutputStream.WriteAsync(Encoding.UTF8.GetBytes(text));
}
