namespace Godot.Collections;

// Godot Array<T> wrapper
public class Array<T> : List<T>
{
    public Array() { }
    public Array(IEnumerable<T> items) : base(items) { }
}

// Godot Dictionary
public class Dictionary<TKey, TValue> : System.Collections.Generic.Dictionary<TKey, TValue>
    where TKey : notnull
{
    public Dictionary() { }
}

// Non-generic Godot dictionary used by audio presentation methods.
public class Dictionary : System.Collections.Generic.Dictionary<Variant, Variant>
{
}

// Non-generic Array
public class Array : List<Variant>
{
    public Array() { }
}
