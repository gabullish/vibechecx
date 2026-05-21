# X (Twitter) feed scoring, current as of 2026-05-18
# Source: xai-org/x-algorithm (May 15, 2026 update) + observed behaviour
# Edit this file when X ships algo changes. No code change needed.

## Primary ranking signals (post-May 2026)

- **Replies weighted ~13–27× a like.** Replies are the strongest single signal. A post with 10 replies and 3 likes outranks a post with 200 likes and 0 replies in out-of-network distribution.
- **Video ASR + completion rate** is the primary signal for video posts. X transcribes speech; accounts using clear spoken language in video get measurable reach boosts.
- **Dwell time on the post itself** (not on the profile, not the linked article), time spent reading the tweet body and the reply thread ranks higher than a quick like-and-scroll.
- **In-network vs out-of-network:** high-quality posts from small accounts now get aggressive out-of-network test exposure. One high-dwell, high-reply post can reach an audience 50–100× the follower base.
- **Profile clicks after exposure**, when someone sees a post in the feed and clicks through to the profile, X interprets this as identity-driven content (the reader wanted more from this person). Strong signal.
- **First-reply quality.** The first reply on a post, especially from the author themselves, affects the whole thread's ranking. A substantive self-reply that adds context or asks a follow-up question extends dwell time.

## What the algorithm penalises

- **Repetitive patterns**, same hook phrasing, same format, multiple posts in a short window. X detects template reuse and down-ranks.
- **Pure promotional voice**, content that reads as brand-speak with no personal angle. The algo rewards conversational authenticity over polished copy.
- **Replies that generate no further conversation**, a reply that gets no engagement itself is near-neutral; a dead reply thread is a mild negative.
- **External links in originals**, still down-ranked, though less harshly than 2023. Replies with links are penalised less. Moving links to the first reply on a linkless original is a known workaround.
- **Coordinated engagement patterns**, like pods, RT rings, or same-group accounts engaging in a burst. X detects temporal clustering and discounts it.

## Format-specific weights (relative, all else equal)

Video > carousel/image thread > single image > text-only thread > bare text post

- **Video with spoken word** outperforms silent video, the ASR signal.
- **Threads where each reply adds new information** outperform threads that pad the same point, dwell time compounds across the thread.
- **Images that prove something** (photos of real events, data visualisations, receipts) outperform decorative graphics, profile-click signal is higher.
- **Quote tweets with substantive added commentary** outperform bare quote-retweets. The added commentary needs to be more than 5 words to register.

## Engagement velocity windows

- **0–30 minutes:** makes-or-breaks initial distribution. Replies in this window matter most.
- **0–6 hours:** determines whether the post enters an out-of-network test bucket.
- **6–24 hours:** secondary amplification if the post was already performing.
- **After 24 hours:** ranking effectively frozen. Evergreen content is an exception only for profiles with established algorithmic credibility.

## Implications for operator_actions

When suggesting actions, tie each recommendation to a specific mechanism above. Examples:
- "Post the analysis thread without a link; add the URL in the first self-reply" → avoids the external-link penalty on originals.
- "End the post with a direct question directed at a specific type of person" → invites replies (the 13–27× signal) from a targeted audience instead of generic engagement bait.
- "When you're next at an in-person event, post a video with spoken commentary rather than a photo" → activates the ASR+completion signal that photo posts cannot trigger.
- "Write the first self-reply within 2 minutes of posting to seed the dwell-time window" → the self-reply extends reading time and signals to X that the author considers this post substantive.
