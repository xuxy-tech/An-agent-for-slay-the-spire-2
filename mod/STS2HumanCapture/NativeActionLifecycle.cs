using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Runs;

namespace STS2HumanCapture;

// This is a game-thread observation of the native action, rather than an
// inference from which UI happens to be visible. The action remains retained
// across its player-choice pause and the continuation after that choice.
internal static class NativeActionLifecycle
{
    private static ActionQueueSet? _queue;
    private static readonly List<GameAction> Recent = [];
    private static long _epoch;
    private static long _revision;

    public static void Attach(ActionQueueSet? queue)
    {
        if (ReferenceEquals(queue, _queue)) return;
        if (_queue != null)
        {
            _queue.ActionEnqueued -= Enqueued;
            _queue.ActionQueueChanged -= Changed;
        }
        _queue = queue;
        Recent.Clear();
        _epoch++;
        _revision++;
        if (_queue != null)
        {
            _queue.ActionEnqueued += Enqueued;
            _queue.ActionQueueChanged += Changed;
        }
    }

    private static void Changed() => _revision++;

    private static void Enqueued(GameAction action)
    {
        _revision++;
        if (!ActionQueueSet.IsGameActionPlayerDriven(action)
            || RunManager.Instance.NetService is { } net && action.OwnerId != net.NetId)
            return;
        Recent.Add(action);
        if (Recent.Count > 64) Recent.RemoveAt(0);
        action.BeforeExecuted += PhaseChanged;
        action.BeforePausedForPlayerChoice += PhaseChanged;
        action.BeforeReadyToResumeAfterPlayerChoice += PhaseChanged;
        action.BeforeResumedAfterPlayerChoice += PhaseChanged;
        action.AfterFinished += PhaseChanged;
        action.BeforeCancelled += PhaseChanged;
    }

    private static void PhaseChanged(GameAction _) => _revision++;

    public static object Capture()
    {
        Attach(RunManager.Instance.ActionQueueSet);
        return new
        {
            schema = "sts2.native_action_lifecycle.v1",
            epoch = _epoch,
            revision = _revision,
            next_action_id = _queue?.NextActionId,
            queue_empty = _queue?.IsEmpty,
            actions = Recent.Select(action => new
            {
                id = action.Id,
                semantic_action = action switch
                {
                    PlayCardAction => "play_card",
                    UsePotionAction => "use_potion",
                    EndPlayerTurnAction => "end_turn",
                    _ => "game_action",
                },
                card_id = action is PlayCardAction card ? card.CardModelId.Entry : null,
                state = action.State.ToString(),
                status = action.Exception != null || action.State.ToString() == "Canceled"
                    ? "failed"
                    : action.State.ToString() == "GatheringPlayerChoice"
                        ? "awaiting_input"
                        : action.State.ToString() == "Finished" && action.CompletionTask.IsCompleted
                            ? "completed" : "running",
                pause_type = action.State.ToString() == "GatheringPlayerChoice"
                    ? "player_choice" : null,
                completion_finished = action.CompletionTask.IsCompleted,
                failed = action.Exception != null,
            }).ToArray(),
        };
    }
}
