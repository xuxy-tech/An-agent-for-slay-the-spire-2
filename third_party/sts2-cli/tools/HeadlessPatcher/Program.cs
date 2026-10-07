using Mono.Cecil;
using Mono.Cecil.Cil;

if (args.Length != 1)
{
    Console.Error.WriteLine("Usage: HeadlessPatcher <sts2.dll>");
    return 2;
}

var dllPath = Path.GetFullPath(args[0]);
var resolver = new DefaultAssemblyResolver();
var libDir = Path.GetDirectoryName(dllPath)!;
resolver.AddSearchDirectory(libDir);

var stubsDir = Path.Combine(
    Path.GetDirectoryName(libDir)!, "src", "GodotStubs", "bin", "Debug", "net9.0"
);
if (Directory.Exists(stubsDir))
{
    resolver.AddSearchDirectory(stubsDir);
}

using var module = ModuleDefinition.ReadModule(
    dllPath,
    new ReaderParameters { AssemblyResolver = resolver, ReadingMode = ReadingMode.Deferred }
);

var patches = 0;
foreach (var type in module.Types)
{
    foreach (var nested in type.NestedTypes)
    {
        foreach (var nested2 in nested.NestedTypes)
        {
            if (!nested2.Name.Contains("YieldAwaiter") && nested2.Name != "<>c")
            {
                continue;
            }

            foreach (var method in nested2.Methods)
            {
                if (method.Name != "get_IsCompleted" || method.Body is null)
                {
                    continue;
                }
                var il = method.Body.GetILProcessor();
                il.Body.Instructions.Clear();
                il.Emit(OpCodes.Ldc_I4_1);
                il.Emit(OpCodes.Ret);
                patches++;
            }
        }
    }
}

foreach (var type in module.Types)
{
    foreach (var method in type.Methods)
    {
        if (method.Name != "WaitUntilQueueIsEmptyOrWaitingOnNonPlayerDrivenAction" || method.Body is null)
        {
            continue;
        }
        var il = method.Body.GetILProcessor();
        il.Body.Instructions.Clear();
        var completedTask = module.ImportReference(
            typeof(Task).GetProperty("CompletedTask")!.GetGetMethod()!
        );
        il.Emit(OpCodes.Call, completedTask);
        il.Emit(OpCodes.Ret);
        patches++;
    }
}

// Test mode skips the official potion price jitter, which also skips one
// PlayerRng.Shops draw per potion. Restore that gameplay path in the headless
// assembly while keeping TestMode enabled for UI and timing suppression.
var potionEntry = module.Types.FirstOrDefault(type =>
    type.FullName == "MegaCrit.Sts2.Core.Entities.Merchant.MerchantPotionEntry");
var potionCalcCost = potionEntry?.Methods.FirstOrDefault(method =>
    method.Name == "CalcCost" && method.Body is not null);
var testModeGuard = potionCalcCost?.Body.Instructions.FirstOrDefault(instruction =>
    instruction.OpCode == OpCodes.Call
    && instruction.Operand is MethodReference method
    && method.FullName == "System.Boolean MegaCrit.Sts2.Core.TestSupport.TestMode::get_IsOff()");
if (testModeGuard is null)
{
    Console.Error.WriteLine("MerchantPotionEntry.CalcCost TestMode guard was not found.");
    return 4;
}
testModeGuard.OpCode = OpCodes.Ldc_I4_1;
testModeGuard.Operand = null;
patches++;

if (patches == 0)
{
    Console.Error.WriteLine("No expected headless IL patch points were found.");
    return 3;
}

var outputPath = dllPath + ".patched";
module.Write(outputPath);
// Cecil keeps the input DLL open in deferred-reading mode. Release that handle
// before replacing the file, otherwise File.Move fails on Windows.
module.Dispose();
File.Move(outputPath, dllPath, overwrite: true);
Console.WriteLine($"Applied {patches} headless patches to {dllPath}");
return 0;
