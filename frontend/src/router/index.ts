import { createRouter, createWebHistory } from 'vue-router'
import CreateView from '../views/CreateView.vue'
import DesignView from '../views/DesignView.vue'
import GalleryView from '../views/GalleryView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'create', component: CreateView },
    { path: '/design/:id', name: 'design', component: DesignView },
    { path: '/gallery', name: 'gallery', component: GalleryView },
  ],
})
