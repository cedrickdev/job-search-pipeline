<!-- Score pill. Ported from webapp/src/components/ScoreChip.tsx. -->
<script setup lang="ts">
import { computed } from 'vue'
import type { Maybe } from '~/types/domain'
import { scoreTone } from '~/utils/status'

const props = defineProps<{ score: Maybe<number> }>()

const tone = computed(() => scoreTone(props.score))
</script>

<template>
  <span class="score-chip" :class="tone">{{ score === null ? '—' : score }}</span>
</template>
