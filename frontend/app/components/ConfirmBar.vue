<!--
  The confirmation gate every destructive drawer action passes through. Ported
  from webapp/src/components/ConfirmBar.tsx.

  `role="alertdialog"` is kept: it is what tells a screen reader that this bar
  demands a decision rather than merely reporting one, and the drawer tests
  select it by that role.
-->
<script setup lang="ts">
defineProps<{ label: string, pending?: boolean }>()

defineEmits<{ confirm: [], cancel: [] }>()
</script>

<template>
  <div class="confirm-bar" role="alertdialog" aria-label="Confirm action">
    <span>{{ label }} — proceed?</span>
    <div>
      <button class="btn-primary" :disabled="pending" @click="$emit('confirm')">
        Confirm
      </button>
      <button class="btn-ghost" :disabled="pending" @click="$emit('cancel')">
        Cancel
      </button>
    </div>
  </div>
</template>
