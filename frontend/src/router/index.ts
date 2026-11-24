import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import RegionPublicationView from '@/views/RegionPublicationView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/region-publication', name: 'region-publication', component: RegionPublicationView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
