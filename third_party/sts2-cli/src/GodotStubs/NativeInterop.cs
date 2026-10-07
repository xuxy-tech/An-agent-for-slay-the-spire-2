namespace Godot.NativeInterop;

using Godot;

public static class VariantUtils
{
    public static T ConvertTo<T>(Variant v) => default!;
    public static Variant CreateFrom<T>(T value) => new Variant(value);
    public static Variant CreateFromString(string s) => new Variant(s);
    public static Variant CreateFromFloat(float f) => new Variant(f);
    public static Variant CreateFromDouble(double d) => new Variant(d);
    public static Variant CreateFromInt(int i) => new Variant(i);
    public static Variant CreateFromBool(bool b) => new Variant(b);
    public static Variant CreateFromStringName(StringName s) => new Variant(s);
}

// Low-level native interop types used in generated bridge code
public struct godot_string_name { }
public struct godot_variant { }
public struct godot_bool { }
public struct NativeGodotVariant { }
public struct NativeGodotString { }
public struct NativeGodotStringName { }

// NativeVariantPtrArgs - used in signal dispatch bridge
public readonly struct NativeVariantPtrArgs
{
    public readonly ref godot_variant this[int index] => ref System.Runtime.CompilerServices.Unsafe.NullRef<godot_variant>();
    public int Count => 0;
}
