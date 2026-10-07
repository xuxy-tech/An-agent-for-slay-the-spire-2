using System.Collections;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Runs;

namespace STS2RngBridge;

[ModInitializer("Initialize")]
public static class Entry
{
    private static int _started;

    public static void Initialize()
    {
        if (Interlocked.Exchange(ref _started, 1) != 0)
            return;
        GameThread.Initialize();
        RngHttpServer.Start();
        Log.Info("[STS2RngBridge] Ready on http://127.0.0.1:9877/", 2);
    }
}

internal static class GameThread
{
    private static SynchronizationContext? _context;
    private static int _threadId;

    public static void Initialize()
    {
        _context = SynchronizationContext.Current
            ?? throw new InvalidOperationException("STS2RngBridge could not capture the game SynchronizationContext");
        _threadId = Environment.CurrentManagedThreadId;
    }

    public static Task<T> InvokeAsync<T>(Func<T> action)
    {
        if (_context == null)
            throw new InvalidOperationException("STS2RngBridge game thread is not initialized");
        if (Environment.CurrentManagedThreadId == _threadId)
            return Task.FromResult(action());
        var completion = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        _context.Post(_ =>
        {
            try { completion.TrySetResult(action()); }
            catch (Exception ex) { completion.TrySetException(ex); }
        }, null);
        return completion.Task;
    }
}

internal static class RngHttpServer
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
    private static HttpListener? _listener;

    public static void Start()
    {
        _listener = new HttpListener();
        _listener.Prefixes.Add("http://127.0.0.1:9877/");
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
        var requestId = $"rng_{DateTime.UtcNow:yyyyMMdd_HHmmss_ffff}_{Environment.TickCount64}";
        try
        {
            if (!string.Equals(context.Request.HttpMethod, "GET", StringComparison.OrdinalIgnoreCase))
            {
                await WriteAsync(context.Response, 405, new { ok = false, request_id = requestId, error = "method_not_allowed" });
                return;
            }
            var path = context.Request.Url?.AbsolutePath;
            object data = path switch
            {
                "/health" => RngSnapshotService.BuildHealth(),
                "/identity" => RngSnapshotService.BuildIdentity(),
                "/rng" => await GameThread.InvokeAsync(RngSnapshotService.Capture),
                _ => throw new KeyNotFoundException(path),
            };
            await WriteAsync(context.Response, 200, new { ok = true, request_id = requestId, data });
        }
        catch (KeyNotFoundException)
        {
            await WriteAsync(context.Response, 404, new { ok = false, request_id = requestId, error = "not_found" });
        }
        catch (Exception ex)
        {
            Log.Error($"[STS2RngBridge] Request failed: {ex}", 2);
            await WriteAsync(context.Response, 500, new { ok = false, request_id = requestId, error = ex.Message });
        }
        finally
        {
            context.Response.Close();
        }
    }

    private static async Task WriteAsync(HttpListenerResponse response, int status, object payload)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(payload, JsonOptions);
        response.StatusCode = status;
        response.ContentType = "application/json; charset=utf-8";
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
    }
}

internal static class RngSnapshotService
{
    public static object BuildHealth() => new
    {
        service = "sts2-rng-bridge",
        protocol_version = "2026-09-19-rng-v1",
        status = "ready",
    };

    public static object BuildIdentity()
    {
        var gameAssembly = typeof(RunManager).Assembly;
        var observerAssembly = AppDomain.CurrentDomain.GetAssemblies()
            .FirstOrDefault(assembly => assembly.GetName().Name == "STS2AIAgent");
        return new SortedDictionary<string, object?>
        {
            ["game_assembly"] = DescribeAssembly(gameAssembly),
            ["observer_assembly"] = observerAssembly == null ? null : DescribeAssembly(observerAssembly),
            ["rng_bridge_assembly"] = DescribeAssembly(typeof(Entry).Assembly),
        };
    }

    public static object Capture()
    {
        var runState = RunManager.Instance.DebugOnlyGetState();
        var run = CaptureSet(runState?.Rng);
        var players = new List<object?>();
        if (runState?.Players != null)
        {
            foreach (var player in runState.Players)
            {
                players.Add(new SortedDictionary<string, object?>
                {
                    ["net_id"] = ReadMember(player, "NetId")?.ToString(),
                    ["streams"] = CaptureSet(ReadMember(player, "PlayerRng")),
                });
            }
        }
        var payload = new SortedDictionary<string, object?>
        {
            ["schema_version"] = 1,
            ["complete"] = runState != null && IsComplete(run) && players.All(IsPlayerComplete),
            ["run_seed"] = ReadMember(runState?.Rng, "StringSeed")?.ToString(),
            ["run_streams"] = run,
            ["players"] = players,
        };
        payload["digest_sha256"] = Digest(payload);
        return payload;
    }

    private static SortedDictionary<string, object?> CaptureSet(object? rngSet)
    {
        var result = new SortedDictionary<string, object?>(StringComparer.Ordinal);
        var dict = ReadMember(rngSet, "_rngs") as IDictionary;
        if (dict == null)
            return result;
        var entries = new List<(string Key, object? Value)>();
        foreach (var item in (IEnumerable)dict)
        {
            if (item is DictionaryEntry dictionaryEntry)
            {
                entries.Add((dictionaryEntry.Key?.ToString() ?? "unknown", dictionaryEntry.Value));
                continue;
            }
            entries.Add((ReadMember(item, "Key")?.ToString() ?? "unknown", ReadMember(item, "Value")));
        }
        foreach (var entry in entries.OrderBy(entry => entry.Key, StringComparer.Ordinal))
        {
            var rng = entry.Value;
            var random = ReadMember(rng, "_random");
            result[entry.Key] = new SortedDictionary<string, object?>
            {
                ["counter"] = ToInt(ReadMember(rng, "Counter") ?? ReadMember(rng, "<Counter>k__BackingField")),
                ["seed"] = ToUInt(ReadMember(rng, "Seed") ?? ReadMember(rng, "<Seed>k__BackingField")),
                ["s0"] = ToULong(ReadMember(random, "_s0")),
                ["s1"] = ToULong(ReadMember(random, "_s1")),
                ["s2"] = ToULong(ReadMember(random, "_s2")),
                ["s3"] = ToULong(ReadMember(random, "_s3")),
            };
        }
        return result;
    }

    private static bool IsComplete(SortedDictionary<string, object?> streams) =>
        streams.Count > 0 && streams.Values.All(value =>
        {
            if (value is not SortedDictionary<string, object?> row)
                return false;
            return row["counter"] != null && row["seed"] != null && row["s0"] != null
                && row["s1"] != null && row["s2"] != null && row["s3"] != null;
        });

    private static bool IsPlayerComplete(object? value)
    {
        if (value is not SortedDictionary<string, object?> player
            || player["streams"] is not SortedDictionary<string, object?> streams)
            return false;
        return IsComplete(streams);
    }

    private static object DescribeAssembly(Assembly assembly)
    {
        var path = assembly.Location;
        return new SortedDictionary<string, object?>
        {
            ["name"] = assembly.GetName().Name,
            ["version"] = assembly.GetName().Version?.ToString(),
            ["sha256"] = File.Exists(path) ? Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant() : null,
        };
    }

    private static string Digest(object payload)
    {
        var json = JsonSerializer.Serialize(payload, new JsonSerializerOptions(JsonSerializerDefaults.Web));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json))).ToLowerInvariant();
    }

    private static object? ReadMember(object? instance, string name)
    {
        if (instance == null)
            return null;
        var type = instance.GetType();
        for (var current = type; current != null; current = current.BaseType)
        {
            var field = current.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (field != null)
                return field.GetValue(instance);
            var property = current.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (property != null)
                return property.GetValue(instance);
        }
        return null;
    }

    private static int? ToInt(object? value) => value == null ? null : Convert.ToInt32(value);
    private static uint? ToUInt(object? value) => value == null ? null : Convert.ToUInt32(value);
    private static ulong? ToULong(object? value) => value == null ? null : Convert.ToUInt64(value);
}
