<script setup lang="ts">
// The V2 opportunity map is the active catalog. The former overview remains at
// its legacy route during migration, but the root must never make a populated V2
// account appear empty by querying V1's separate data store.
definePageMeta({ middleware: 'auth' })

await navigateTo('/map')
</script>
