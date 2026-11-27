"""Reconcile native effects with the atomically committed state generation."""

from .store import OperationError


def apply(native, state, effect):
    if effect == "schedule":
        result = native.schedule(state["enabled"] and state["failover"], state["interval"])
    elif effect == "forwarding":
        result = native.apply(state)
    else:
        raise OperationError("Invalid pending service operation")
    if result is False:
        raise OperationError("Native service activation failed")


def recover(store, native):
    pending = store.pending()
    if pending is None:
        return None
    try:
        if not isinstance(pending, dict) or set(pending) != {"previous", "candidate", "effect"}:
            raise ValueError
        generations = (pending["previous"], pending["candidate"])
        for identifier in generations:
            store.path(identifier)
        committed = store.current()
        if committed not in generations:
            raise ValueError
        apply(native, store.read(committed), pending["effect"])
        store.clear_pending()
    except Exception:
        raise OperationError("Proxy recovery failed. Inspect service state and retry recovery") from None
    store.prune(committed)
    return pending["effect"]


def publish(store, native, previous, state, effect):
    candidate = store.stage(state)
    if effect:
        store.write_pending(previous, candidate, effect)
        try:
            apply(native, state, effect)
            store.commit(candidate)
        except Exception:
            # A failed directory sync can occur after the pointer was replaced.
            # Re-read it to choose the correct side of the commit point.
            recover(store, native)
            raise OperationError("Proxy change failed. Inspect the effective settings") from None
        store.clear_pending()
    else:
        store.commit(candidate)
    store.prune(candidate)
