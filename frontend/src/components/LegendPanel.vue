<script setup lang="ts">
// Static legend component - no props needed
</script>

<template>
  <div class="legend-panel">
    <div class="legend-title">Node Types</div>

    <div class="legend-item">
      <svg width="36" height="24" viewBox="0 0 36 24">
        <polygon
          points="18,4 30,12 18,20 6,12"
          fill="#FFFFFF"
          stroke="#2C2C2C"
          stroke-width="1.2"
        />
      </svg>
      <span class="legend-label">User Addresses</span>
    </div>

    <div class="legend-item">
      <svg width="36" height="24" viewBox="0 0 36 24">
        <ellipse
          cx="18"
          cy="12"
          rx="13"
          ry="7"
          fill="#4DD0E1"
          stroke="#2C2C2C"
          stroke-width="1.2"
        />
      </svg>
      <span class="legend-label">ERC20 Tokens</span>
    </div>

    <div class="legend-item">
      <svg width="36" height="24" viewBox="0 0 36 24">
        <rect
          x="6"
          y="6"
          width="24"
          height="12"
          rx="1"
          fill="#FF9E9E"
          stroke="#2C2C2C"
          stroke-width="1.2"
        />
      </svg>
      <span class="legend-label">Contracts</span>
    </div>
  </div>
</template>

<style scoped>
.legend-panel {
  position: absolute;
  top: 36px;
  right: 12px;
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 8px 10px;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
  z-index: 100;
  min-width: 160px;
}

.legend-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.6px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.legend-item:last-child {
  margin-bottom: 0;
}

.legend-label {
  font-size: 11px;
  color: var(--text);
}

svg {
  flex-shrink: 0;
}
</style>
