// The write half of the data layer.
//
// V1's components read five things off every TanStack mutation: `isPending` (to
// disable a button), `error`, `data` (the follow-up panel renders the draft the
// server returned), `isSuccess` (Settings shows "Saved.") and
// `mutate`/`mutateAsync`. This reproduces exactly that surface over a plain async
// function, so the migrated components keep the same shape without pulling in a
// query library.
//
// The returned object is `reactive` rather than a bag of refs so a template can
// write `draft.isPending` instead of `draft.isPending.value` — refs nested inside
// a plain object are not unwrapped in templates, and every call site here is
// nested (`const { snooze, draft } = useFollowupActions()`).
//
// Deliberately absent: retries, optimistic updates and rollback. V1 had none of
// them, and the actions here change application state on a real backend — a
// silent retry of `POST /api/jobs/{id}/applied` is a duplicate submission.
import { reactive, ref, shallowRef } from 'vue'

export interface MutationOptions<TArgs, TResult> {
  /** Runs after a successful call, before `mutateAsync` resolves. */
  onSuccess?: (result: TResult, args: TArgs) => void | Promise<void>
}

export function useMutation<TArgs, TResult>(
  fn: (args: TArgs) => Promise<TResult>,
  options: MutationOptions<TArgs, TResult> = {},
) {
  const isPending = ref(false)
  // TanStack's `isSuccess` latches after the first successful call and resets on
  // the next attempt. Settings renders "Saved." off it.
  const isSuccess = ref(false)
  // shallowRef: the payload is data to render, not something to make deeply
  // reactive, and an Error in `error` should stay the Error it was.
  const error = shallowRef<unknown>(null)
  const data = shallowRef<TResult | null>(null)

  /** Runs the mutation, resolving to the result or rejecting as the API did. */
  async function mutateAsync(args: TArgs): Promise<TResult> {
    isPending.value = true
    isSuccess.value = false
    error.value = null
    try {
      const result = await fn(args)
      data.value = result
      await options.onSuccess?.(result, args)
      isSuccess.value = true
      return result
    } catch (e) {
      error.value = e
      throw e
    } finally {
      isPending.value = false
    }
  }

  /**
   * Fire and forget, folding the rejection into `error`. Templates call this from
   * `@click`, where an unhandled rejection would surface as a console error with
   * nothing shown to the user.
   */
  function mutate(args: TArgs): void {
    void mutateAsync(args).catch(() => {})
  }

  return reactive({ mutate, mutateAsync, isPending, isSuccess, error, data })
}

export type Mutation<TArgs, TResult> = ReturnType<typeof useMutation<TArgs, TResult>>
