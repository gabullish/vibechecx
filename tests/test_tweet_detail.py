"""Tweet detail page + reply-count link on /posts."""


def _seed_tweet_with_replies(db, account_id, tweet_id, n_replies=3):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, "
            "is_retweet, replies, last_measured_at) "
            "VALUES (%s, %s, NOW(), %s, FALSE, %s, NOW())",
            (tweet_id, account_id, f"parent {tweet_id}", n_replies),
        )
        for i in range(n_replies):
            cur.execute(
                "INSERT INTO accounts(username) VALUES(%s) "
                "ON CONFLICT(username) DO UPDATE SET username=EXCLUDED.username RETURNING id",
                (f"replier_{tweet_id}_{i}",),
            )
            rid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO replies(tweet_id, reply_id, author_account_id, content, "
                "created_at, likes, sentiment) "
                "VALUES (%s, %s, %s, %s, NOW(), %s, %s)",
                (tweet_id, f"r_{tweet_id}_{i}", rid, f"reply #{i}", 10 - i, 0.5 - i * 0.3),
            )
    db.commit()


def test_tweet_detail_renders_mined_replies(client, make_user, make_single_profile, db):
    u = make_user("alice", "pw12345")
    pid = make_single_profile(u["id"], handle="alpha")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE username='alpha'")
        aid = cur.fetchone()["id"]
    _seed_tweet_with_replies(db, aid, "tw_42", n_replies=3)

    client.login("alice", "pw12345")
    r = client.get("/tweet/tw_42")
    assert r.status_code == 200
    assert "parent tw_42" in r.text
    assert "reply #0" in r.text
    assert "reply #1" in r.text
    # Replies are sorted by likes DESC, so #0 (10 likes) is first
    assert r.text.index("reply #0") < r.text.index("reply #1") < r.text.index("reply #2")
    # The "Mined replies" header shows counts
    assert "3 stored" in r.text


def test_tweet_detail_empty_state_when_no_replies(client, make_user, make_single_profile, db):
    u = make_user("bob", "pw12345")
    make_single_profile(u["id"], handle="quiet")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE username='quiet'")
        aid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, "
            "is_retweet) VALUES (%s, %s, NOW(), %s, FALSE)",
            ("tw_quiet", aid, "alone"),
        )
    db.commit()
    client.login("bob", "pw12345")
    r = client.get("/tweet/tw_quiet")
    assert r.status_code == 200
    assert "No replies mined yet" in r.text


def test_posts_row_links_to_tweet_detail(client, make_user, make_single_profile, db):
    u = make_user("carol", "pw12345")
    pid = make_single_profile(u["id"], handle="link")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE username='link'")
        aid = cur.fetchone()["id"]
    _seed_tweet_with_replies(db, aid, "tw_linktest", n_replies=2)
    client.login("carol", "pw12345")
    client.get(f"/set-profile/{pid}", follow_redirects=False)
    r = client.get("/posts")
    assert r.status_code == 200
    assert '/tweet/tw_linktest' in r.text


def test_posts_type_filter_replies_only(client, make_user, make_single_profile, db):
    u = make_user("dave", "pw12345")
    pid = make_single_profile(u["id"], handle="filter")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE username='filter'")
        aid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, "
            "is_retweet, is_reply) VALUES "
            "('orig_1', %s, NOW(), 'an original', FALSE, FALSE), "
            "('rep_1',  %s, NOW(), 'a reply',     FALSE, TRUE)",
            (aid, aid),
        )
    db.commit()
    client.login("dave", "pw12345")
    client.get(f"/set-profile/{pid}", follow_redirects=False)

    r = client.get("/posts?type=replies")
    assert "a reply" in r.text
    assert "an original" not in r.text

    r = client.get("/posts?type=originals")
    assert "an original" in r.text
    assert "a reply" not in r.text
