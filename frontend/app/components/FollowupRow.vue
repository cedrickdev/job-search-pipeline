<!--
  One follow-up row. Ported from the inner `FollowupRow` of
  webapp/src/components/FollowupsPanel.tsx.

  It is a separate component for the reason V1 made it one: each row owns its own
  mutation instances, so a Draft/Snooze/Dismiss in flight on one follow-up cannot
  disable or overwrite its siblings' buttons.

  A draft that fails the mandate gate is shown with its flags rather than
  withheld — the user still needs to see what the template produced — but it is
  labelled unverified so it is never mistaken for something ready to send.
-->
<script setup lang="ts">
import type { FollowupItem } from '~/types/domain'
import { useFollowupActions } from '~/composables/useMutations'

const props = defineProps<{ item: FollowupItem }>()
const emit = defineEmits<{ open: [jobId: number] }>()

const REASON: Record<FollowupItem['kind'], string> = {
  applied_no_reply: 'no reply',
  recruiter_no_outbound: 'no outbound',
}

const { snooze, dismiss, draft } = useFollowupActions()

function copy() {
  const d = draft.data
  if (!d) return
  navigator.clipboard?.writeText(`${d.subject}\n\n${d.body}`)
}
</script>

<template>
  <li class="fu-item">
    <div class="fu-head">
      <button type="button" class="fu-main" @click="emit('open', props.item.job_id)">
        <span class="job-row-company">{{ props.item.company }}</span>
        <span class="job-row-title">{{ props.item.title }}</span>
      </button>
      <span class="ov-when">{{ props.item.days }}d · {{ REASON[props.item.kind] }}</span>
    </div>

    <div class="fu-actions">
      <button type="button" :disabled="draft.isPending" @click="draft.mutate(props.item.job_id)">
        {{ draft.isPending ? 'Drafting…' : 'Draft' }}
      </button>
      <button
        type="button"
        :disabled="snooze.isPending"
        @click="snooze.mutate({ jobId: props.item.job_id })"
      >
        Snooze 7d
      </button>
      <button type="button" :disabled="dismiss.isPending" @click="dismiss.mutate(props.item.job_id)">
        Dismiss
      </button>
    </div>

    <div v-if="draft.data" class="fu-draft">
      <p v-if="!draft.data.mandate_ok" class="fu-warn">
        Draft not verified ({{ draft.data.flags.join(', ') || 'compliance gate' }}) — review before
        sending.
      </p>
      <p class="fu-subject">
        {{ draft.data.subject }}
      </p>
      <pre class="fu-body">{{ draft.data.body }}</pre>
      <button type="button" class="fu-copy" @click="copy">
        Copy
      </button>
    </div>
  </li>
</template>
