# Trains en direct – Île-de-France

1. Envoyez tous les fichiers sur GitHub (commit sur `main`), sans oublier le dossier caché `.github`.
2. Settings > Pages > Source : **GitHub Actions**.
3. Settings > Secrets and variables > Actions > New repository secret : `PRIM_API_KEY` (jeton PRIM, facultatif : sert aux incidents).
4. Onglet Actions > « Site trains IDF » > Run workflow, cochez « Tout reconstruire » (première fois : plusieurs minutes).
5. Le site est publié à l'adresse `https://VOTRE-NOM.github.io/NOM-DU-DEPOT/`.

Le site fonctionne sans jeton (horaires théoriques). Le temps réel est facultatif : voir `config.js`.
En cas de souci, ouvrez « Diagnostic » en bas du menu de la page.
