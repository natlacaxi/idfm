const V='trains-idf-v3',SHELL=['./','index.html','manifest.webmanifest','config.js','icons/icon-192.png','icons/icon-512.png','icons/favicon-64.png'];
const keep=(req,r)=>{if(r.ok){const c=r.clone();caches.open(V).then(x=>x.put(req,c))}return r};
// un fichier manquant ne doit jamais empêcher l'installation : on ajoute chaque fichier séparément
self.addEventListener('install',e=>e.waitUntil(caches.open(V).then(c=>Promise.allSettled(SHELL.map(u=>c.add(u)))).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==V).map(x=>caches.delete(x)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
  const q=e.request,u=new URL(q.url);if(q.method!=='GET')return;
  if(u.origin===location.origin){ // réseau d'abord (données fraîches), cache si hors ligne
    e.respondWith(fetch(q).then(r=>keep(q,r)).catch(()=>caches.match(q).then(r=>r||caches.match('index.html'))));return}
  if(u.hostname==='cdnjs.cloudflare.com'||u.hostname.startsWith('fonts.g')) // bibliothèques : cache d'abord
    e.respondWith(caches.match(q).then(r=>r||fetch(q).then(n=>keep(q,n))));
});
