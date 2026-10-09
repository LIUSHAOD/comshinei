<script setup lang="ts">
</script>

<template>
  <div class="app-shell">
    <header class="nav">
      <div class="container nav-inner">
        <router-link to="/" class="brand">Comshinei<span>室内设计 Agent</span></router-link>
        <nav class="nav-links">
          <router-link to="/" class="nav-link" exact-active-class="active">创建</router-link>
          <router-link to="/gallery" class="nav-link" active-class="active">风格库</router-link>
        </nav>
      </div>
    </header>
    <main class="container main">
      <router-view :key="$route.fullPath" />
    </main>
  </div>
</template>
