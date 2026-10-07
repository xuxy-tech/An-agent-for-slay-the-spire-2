using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using MegaCrit.Sts2.Core.Runs;

namespace STS2HumanCapture;

/// <summary>
/// Captures the live game's own serializable run at a settled boundary.
/// </summary>
internal static class AuthoritativeRunSaves
{
    public const string Schema = "sts2.run_save.authoritative.v1";

    public static SortedDictionary<string, object?> Capture()
    {
        var runState = RunManager.Instance.DebugOnlyGetState()
            ?? throw new InvalidOperationException("No run in progress");
        var room = ReadMember(runState, "CurrentRoom");
        var serialized = InvokeToSave(room)
            ?? throw new InvalidOperationException("RunManager.ToSave returned null");
        var json = InvokeToJson(serialized);
        var bytes = Encoding.UTF8.GetBytes(json);
        return new(StringComparer.Ordinal)
        {
            ["schema"] = Schema,
            ["captured_at_utc"] = DateTime.UtcNow.ToString("O"),
            ["sha256"] = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant(),
            ["bytes"] = bytes.Length,
            ["room_type"] = room?.GetType().FullName,
            ["save_json"] = json,
        };
    }

    private static object? InvokeToSave(object? room)
    {
        var runManager = RunManager.Instance;
        var methods = runManager.GetType().GetMethods(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .Where(m => m.Name == "ToSave").ToArray();
        foreach (var method in methods.Where(m => m.GetParameters().Length == 1))
        {
            var parameters = method.GetParameters();
            var parameterType = parameters[0].ParameterType;
            if (room == null || parameterType.IsInstanceOfType(room))
            {
                try
                {
                    return Unwrap(method.Invoke(runManager, new[] { room }));
                }
                catch (TargetInvocationException ex) when (ex.InnerException is NullReferenceException)
                {
                    // Match the game's lazy serialization warmup behavior.
                    return Unwrap(method.Invoke(runManager, new[] { room }));
                }
            }
        }
        foreach (var method in methods.Where(m => m.GetParameters().Length == 0))
        {
            try
            {
                return Unwrap(method.Invoke(runManager, null));
            }
            catch (TargetInvocationException ex) when (ex.InnerException is NullReferenceException)
            {
                return Unwrap(method.Invoke(runManager, null));
            }
        }
        throw new MissingMethodException(runManager.GetType().FullName, "ToSave");
    }

    private static string InvokeToJson(object serializable)
    {
        var saveManager = typeof(MegaCrit.Sts2.Core.Saves.SaveManager);
        foreach (var method in saveManager.GetMethods(
            BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic))
        {
            var serializer = method;
            if (serializer.IsGenericMethodDefinition)
            {
                if (serializer.GetGenericArguments().Length != 1)
                    continue;
                serializer = serializer.MakeGenericMethod(serializable.GetType());
            }
            var parameters = serializer.GetParameters();
            if (serializer.Name != "ToJson" || parameters.Length != 1 ||
                !parameters[0].ParameterType.IsInstanceOfType(serializable))
                continue;
            var result = serializer.Invoke(null, new[] { serializable });
            if (result is string json && !string.IsNullOrWhiteSpace(json))
                return json;
        }
        throw new MissingMethodException(saveManager.FullName, "ToJson");
    }

    private static object? Unwrap(object? value)
    {
        if (value is not Task task)
            return value;
        task.GetAwaiter().GetResult();
        return task.GetType().GetProperty("Result")?.GetValue(task);
    }

    private static object? ReadMember(object value, string name)
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
}
