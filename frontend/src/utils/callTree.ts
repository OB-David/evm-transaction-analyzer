import type { CallTreeEntry, CallTreePayload } from '../api/analyze'

export interface CallTreeFrame extends CallTreeEntry {
  parentCallId: number | null
  depth: number
  order: number
  childCallIds: number[]
  descendantCount: number
}

export interface CallTreeModel {
  totalCalls: number
  rootName: string
  rootCallIds: number[]
  frames: CallTreeFrame[]
  frameById: Map<number, CallTreeFrame>
}

export function buildCallTreeModel(mapping: CallTreePayload): CallTreeModel {
  const orderedCalls = [...mapping.calls].sort((left, right) => (
    left.entry_step - right.entry_step
    || right.exit_step - left.exit_step
    || left.call_id - right.call_id
  ))
  const frameById = new Map<number, CallTreeFrame>()
  const rootCallIds: number[] = []

  orderedCalls.forEach((call, order) => {
    const parent = call.parent_call_id === null
      ? undefined
      : frameById.get(call.parent_call_id)
    if (call.parent_call_id !== null && !parent) {
      throw new Error(`Invalid call_tree.json: missing parent ${call.parent_call_id}`)
    }

    const frame: CallTreeFrame = {
      ...call,
      parentCallId: parent?.call_id ?? null,
      depth: call.depth,
      order: order + 1,
      childCallIds: [],
      descendantCount: 0,
    }
    frameById.set(frame.call_id, frame)

    if (parent) parent.childCallIds.push(frame.call_id)
    else rootCallIds.push(frame.call_id)

  })

  const countDescendants = (callId: number): number => {
    const frame = frameById.get(callId)
    if (!frame) return 0
    frame.descendantCount = frame.childCallIds.reduce(
      (total, childId) => total + 1 + countDescendants(childId),
      0,
    )
    return frame.descendantCount
  }
  rootCallIds.forEach(countDescendants)

  const firstRootId = rootCallIds[0]
  const firstRoot = firstRootId === undefined ? null : frameById.get(firstRootId)
  return {
    totalCalls: frameById.size,
    rootName: mapping.root.name || firstRoot?.from_name || 'Transaction root',
    rootCallIds,
    frames: [...frameById.values()],
    frameById,
  }
}

export function collapsedAtDepth(model: CallTreeModel, maxExpandedDepth: number): Set<number> {
  return new Set(
    model.frames
      .filter(frame => frame.childCallIds.length > 0 && frame.depth >= maxExpandedDepth)
      .map(frame => frame.call_id),
  )
}

export function isDescendantOf(
  model: CallTreeModel,
  possibleDescendantId: number,
  ancestorId: number,
): boolean {
  let current = model.frameById.get(possibleDescendantId)
  while (current?.parentCallId !== null && current?.parentCallId !== undefined) {
    if (current.parentCallId === ancestorId) return true
    current = model.frameById.get(current.parentCallId)
  }
  return false
}
