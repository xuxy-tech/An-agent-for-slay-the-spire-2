using System.Reflection;
using MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;

namespace STS2HumanCapture;

internal static class NativeCrystalSphere
{
    private const BindingFlags Hidden = BindingFlags.Instance | BindingFlags.NonPublic;
    public const string Contract = "sts2.crystal_sphere.v1";

    private static (NCrystalSphereScreen Screen, CrystalSphereMinigame Game)? Active()
    {
        if (NOverlayStack.Instance?.Peek() is not NCrystalSphereScreen screen)
            return null;
        var game = typeof(NCrystalSphereScreen).GetField("_entity", Hidden)?.GetValue(screen)
            as CrystalSphereMinigame;
        if (game == null)
            throw new InvalidOperationException("Crystal Sphere screen has no native minigame");
        return (screen, game);
    }

    public static Dictionary<string, object?> Capture()
    {
        var active = Active();
        if (active == null)
            return new() { ["contract"] = Contract, ["active"] = false };
        var (screen, game) = active.Value;
        var visible = new List<Dictionary<string, object?>>();
        for (var x = 0; x < game.GridSize.X; x++)
        for (var y = 0; y < game.GridSize.Y; y++)
        {
            var cell = game.cells[x, y];
            if (!cell.IsHidden)
                visible.Add(new Dictionary<string, object?>
                {
                    ["x"] = x, ["y"] = y,
                    ["item_kind"] = cell.Item?.GetType().Name,
                });
        }
        var proceed = typeof(NCrystalSphereScreen).GetField("_proceedButton", Hidden)
            ?.GetValue(screen) as NProceedButton;
        var canProceed = game.DivinationCount == 0 && proceed?.IsEnabled == true;
        return new()
        {
            ["contract"] = Contract,
            ["active"] = true,
            ["phase"] = game.DivinationCount > 0 ? "divining"
                : canProceed ? "proceed" : "settling",
            ["width"] = game.GridSize.X,
            ["height"] = game.GridSize.Y,
            ["remaining"] = game.DivinationCount,
            ["visible_cells"] = visible,
        };
    }

    public static Task Click(int x, int y, string tool, int expectedRemaining)
    {
        var (screen, game) = Active()
            ?? throw new InvalidOperationException("Crystal Sphere is not the active overlay");
        if (game.DivinationCount <= 0 || game.DivinationCount != expectedRemaining)
            throw new InvalidOperationException("Crystal Sphere remaining count changed before click");
        if (x < 0 || x >= game.GridSize.X || y < 0 || y >= game.GridSize.Y
            || !game.cells[x, y].IsHidden)
            throw new ArgumentOutOfRangeException(nameof(x), "Crystal Sphere target is not hidden");
        var mode = tool.ToLowerInvariant() switch
        {
            "small" => "SetSmallDivination",
            "big" => "SetBigDivination",
            _ => throw new ArgumentException("Crystal Sphere tool must be small or big"),
        };
        var buttonName = tool.Equals("small", StringComparison.OrdinalIgnoreCase)
            ? "_smallDivinationButton" : "_bigDivinationButton";
        var button = typeof(NCrystalSphereScreen).GetField(buttonName, Hidden)?.GetValue(screen)
            as NButton ?? throw new InvalidOperationException("Crystal Sphere tool button unavailable");
        var selectTool = typeof(NCrystalSphereScreen).GetMethod(mode, Hidden)
            ?? throw new MissingMethodException($"NCrystalSphereScreen.{mode}");
        selectTool.Invoke(screen, new object[] { button });
        var cellsNode = ((Godot.Node)screen).GetNode<Godot.Control>("%Cells");
        var cellNode = cellsNode.GetChildren().OfType<NCrystalSphereCell>()
            .SingleOrDefault(cell => cell.Entity.X == x && cell.Entity.Y == y)
            ?? throw new InvalidOperationException("Crystal Sphere UI cell unavailable");
        var click = typeof(NCrystalSphereScreen).GetMethod("OnCellClicked", Hidden)
            ?? throw new MissingMethodException("NCrystalSphereScreen.OnCellClicked");
        return click.Invoke(screen, new object[] { cellNode }) as Task
            ?? throw new InvalidOperationException("Crystal Sphere click did not return a task");
    }

    public static Task Proceed()
    {
        var (screen, game) = Active()
            ?? throw new InvalidOperationException("Crystal Sphere is not the active overlay");
        var button = typeof(NCrystalSphereScreen).GetField("_proceedButton", Hidden)
            ?.GetValue(screen) as NProceedButton;
        if (game.DivinationCount != 0 || button?.IsEnabled != true)
            throw new InvalidOperationException("Crystal Sphere proceed is not available");
        return MegaCrit.Sts2.Core.Runs.RunManager.Instance.ProceedFromTerminalRewardsScreen();
    }
}
