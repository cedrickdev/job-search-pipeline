<!--
  Follow-ups due, on the overview. Ported from the outer `FollowupsPanel` of
  webapp/src/components/FollowupsPanel.tsx.

  `data-testid` is kept: the overview test locates this section by it, and the
  heading text ("Follow-ups due") is not unique enough to select on.
-->
<script setup lang="ts">
import type { FollowupItem } from '~/types/domain'

const props = defineProps<{ items: FollowupItem[] }>()
const emit = defineEmits<{ open: [jobId: number] }>()
</script>

<template>
  <section class="ov-section" data-testid="followups-due">
    <h2 class="ov-section-title">
      Follow-ups due <span class="ov-count">{{ props.items.length }}</span>
    </h2>
    <p v-if="props.items.length === 0" class="ov-empty">
      No follow-ups due.
    </p>
    <ul v-else class="fu-list">
      <FollowupRow
        v-for="f in props.items"
        :key="f.application_id"
        :item="f"
        @open="emit('open', $event)"
      />
    </ul>
  </section>
</template>
