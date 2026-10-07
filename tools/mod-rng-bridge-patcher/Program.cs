using Mono.Cecil;
using Mono.Cecil.Cil;

if (args.Length == 3 && string.Equals(args[0], "remove", StringComparison.OrdinalIgnoreCase))
{
    var input = Path.GetFullPath(args[1]);
    var output = Path.GetFullPath(args[2]);
    using var observer = AssemblyDefinition.ReadAssembly(input);
    var modEntry = observer.MainModule.GetType("STS2AIAgent.ModEntry")
        ?? throw new InvalidOperationException("STS2AIAgent.ModEntry not found");
    var target = modEntry.Methods.Single(method => method.Name == "Initialize" && method.IsStatic && method.Parameters.Count == 0);
    var calls = target.Body.Instructions.Where(instruction => instruction.Operand is MethodReference method
        && method.DeclaringType.FullName == "STS2RngBridge.Entry" && method.Name == "Initialize").ToArray();
    if (calls.Length == 0)
    {
        Console.Error.WriteLine("Observer DLL has no STS2RngBridge initializer call");
        return 3;
    }
    var processor = target.Body.GetILProcessor();
    foreach (var call in calls)
        processor.Remove(call);
    foreach (var reference in observer.MainModule.AssemblyReferences
                 .Where(reference => reference.Name == "STS2RngBridge").ToArray())
        observer.MainModule.AssemblyReferences.Remove(reference);
    Directory.CreateDirectory(Path.GetDirectoryName(output)!);
    observer.Write(output);
    Console.WriteLine($"Removed STS2RngBridge call from {input} -> {output}");
    return 0;
}

if (args.Length != 3)
{
    Console.Error.WriteLine("Usage: ModRngBridgePatcher <STS2AIAgent.dll> <STS2RngBridge.dll> <output.dll>");
    Console.Error.WriteLine("   or: ModRngBridgePatcher remove <STS2AIAgent.dll> <output.dll>");
    return 2;
}

var patchInput = Path.GetFullPath(args[0]);
var bridgePath = Path.GetFullPath(args[1]);
var patchOutput = Path.GetFullPath(args[2]);
var resolver = new DefaultAssemblyResolver();
resolver.AddSearchDirectory(Path.GetDirectoryName(patchInput)!);
resolver.AddSearchDirectory(Path.GetDirectoryName(bridgePath)!);
using var patchObserver = AssemblyDefinition.ReadAssembly(patchInput, new ReaderParameters { AssemblyResolver = resolver });
using var bridge = AssemblyDefinition.ReadAssembly(bridgePath, new ReaderParameters { AssemblyResolver = resolver });
var entry = bridge.MainModule.GetType("STS2RngBridge.Entry")
    ?? throw new InvalidOperationException("STS2RngBridge.Entry not found");
var initialize = entry.Methods.Single(method => method.Name == "Initialize" && method.IsStatic && method.Parameters.Count == 0);
var patchModEntry = patchObserver.MainModule.GetType("STS2AIAgent.ModEntry")
    ?? throw new InvalidOperationException("STS2AIAgent.ModEntry not found");
var patchTarget = patchModEntry.Methods.Single(method => method.Name == "Initialize" && method.IsStatic && method.Parameters.Count == 0);
if (patchTarget.Body.Instructions.Any(instruction => instruction.Operand is MethodReference method
    && method.DeclaringType.FullName == "STS2RngBridge.Entry" && method.Name == "Initialize"))
{
    Console.Error.WriteLine("Observer DLL is already patched for STS2RngBridge");
    return 3;
}
var imported = patchObserver.MainModule.ImportReference(initialize);
var patchProcessor = patchTarget.Body.GetILProcessor();
patchProcessor.InsertBefore(patchTarget.Body.Instructions[0], patchProcessor.Create(OpCodes.Call, imported));
Directory.CreateDirectory(Path.GetDirectoryName(patchOutput)!);
patchObserver.Write(patchOutput);
Console.WriteLine($"Patched {patchInput} -> {patchOutput}");
return 0;
