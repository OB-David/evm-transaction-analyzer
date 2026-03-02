<script setup lang="ts">
import TitleBar from './components/TitleBar.vue'
import InputPanel from './components/InputPanel.vue'
import CfgPanel from './components/CfgPanel.vue'
import AfgPanel from './components/AfgPanel.vue'
import ReservedPanel from './components/ReservedPanel.vue'
</script>

<template>
  <div class="app-grid">
    <TitleBar class="title-bar" />
    <div class="left-col">
      <InputPanel class="input-panel" />
      <ReservedPanel class="reserved-panel" />
    </div>
    <div class="right-col">
      <CfgPanel class="cfg-panel" />
      <AfgPanel class="afg-panel" />
    </div>
  </div>
</template>

<style scoped>
.app-grid {
  height: 100%;
  display: grid;
  grid-template-columns: 20% 80%;
  grid-template-rows: 36px 1fr;
  gap: 1px;
  background: var(--border);
}

.title-bar {
  grid-column: 1 / -1;
}

.left-col {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--border);
}

.input-panel {
  height: 10vh;
  min-height: 80px;
}

.reserved-panel {
  flex: 1;
}

.right-col {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--border);
}

.cfg-panel {
  flex: 1;
}

.afg-panel {
  flex: 1;
}
</style>
