using Mono.Cecil;

if (args.Length < 3)
{
    Console.Error.WriteLine("Usage: RngIlInspector <assembly> <type-fragment|--calls> <method-or-operand-fragment>");
    return 2;
}
var assemblyPath = Path.GetFullPath(args[0]);
var selector = args[1];
var fragment = args[2];
var resolver = new DefaultAssemblyResolver();
resolver.AddSearchDirectory(Path.GetDirectoryName(assemblyPath)!);
using var assembly = AssemblyDefinition.ReadAssembly(assemblyPath, new ReaderParameters { AssemblyResolver = resolver });
var types = assembly.MainModule.Types.SelectMany(Flatten).ToArray();
if (selector == "--calls")
{
    foreach (var type in types)
    foreach (var method in type.Methods.Where(method => method.HasBody))
    {
        var matches = method.Body.Instructions.Where(instruction =>
            instruction.Operand?.ToString()?.Contains(fragment, StringComparison.OrdinalIgnoreCase) == true).ToArray();
        if (matches.Length == 0) continue;
        Console.WriteLine($"METHOD {method.FullName}");
        foreach (var instruction in matches)
            Console.WriteLine($"  {instruction.Offset:X4}: {instruction.OpCode} {instruction.Operand}");
    }
    return 0;
}
foreach (var type in types.Where(type => type.FullName.Contains(selector, StringComparison.OrdinalIgnoreCase)))
{
    foreach (var method in type.Methods.Where(method => method.HasBody
                 && method.FullName.Contains(fragment, StringComparison.OrdinalIgnoreCase)))
    {
        Console.WriteLine($"TYPE {type.FullName}");
        Console.WriteLine($"METHOD {method.FullName}");
        foreach (var instruction in method.Body.Instructions)
            Console.WriteLine($"  {instruction.Offset:X4}: {instruction.OpCode} {instruction.Operand}");
    }
}
return 0;

static IEnumerable<TypeDefinition> Flatten(TypeDefinition type)
{
    yield return type;
    foreach (var nested in type.NestedTypes.SelectMany(Flatten))
        yield return nested;
}
