# Stop and Learn — public reviewer demo

Children choose what to watch. Parents choose what to practise.

Live demo: https://andytheboat.github.io/StopandLearn.github.io/

This account-free, non-commercial proof of concept demonstrates independent parent-selected sample questions around embedded YouTube playback. No real customers, search.list integration, microphone or persistent learning history. The questions are not based on video content.

## Reviewer pages
- `index.html`: video-link selection, native YouTube player, timed questions and transient practice counts.
- `review.html`: architecture, honest current-feature boundaries and hypothetical traffic forecast.
- `privacy.html` and `terms.html`: notices specific to this static adult-review demo.

The player API loads only after an adult reviewer consents and submits a valid video link. Questions sit beside or below the player. No credentials or real child data belong in this repository.

This is a reduced static demonstration separate from the local Django/PostgreSQL/Vosk prototype. It does not claim YouTube approval or completed production child-service compliance. GitHub Pages is being used for non-commercial project demonstration, not a paid SaaS service.
