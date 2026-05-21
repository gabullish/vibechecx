"""§10.7 — /tweet/{id} renders parent_reply_id children indented under parents."""


def _seed_parent_and_nested_reply(db):
    with db.cursor() as cur:
        cur.execute("INSERT INTO accounts(username) VALUES('author_x') RETURNING id")
        author_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO accounts(username) VALUES('replier_a') RETURNING id")
        a_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO accounts(username) VALUES('replier_b') RETURNING id")
        b_id = cur.fetchone()["id"]
        # parent tweet
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, replies) "
            "VALUES('TW1', %s, NOW(), 'parent post', 2)",
            (author_id,),
        )
        # top-level reply by A (no parent_reply_id → it's a child of the tweet)
        cur.execute(
            "INSERT INTO replies(tweet_id, reply_id, author_account_id, content, created_at, likes) "
            "VALUES('TW1', 'R_top_A', %s, 'top reply by A', NOW(), 10)",
            (a_id,),
        )
        # nested reply by B (parent_reply_id = R_top_A)
        cur.execute(
            "INSERT INTO replies(tweet_id, reply_id, author_account_id, content, created_at, likes, parent_reply_id) "
            "VALUES('TW1', 'R_nested_B', %s, 'nested reply by B', NOW(), 3, 'R_top_A')",
            (b_id,),
        )
    db.commit()


def test_tweet_detail_nests_child_replies_under_parent(client, make_user, db):
    u = make_user("alice", "pw12345")
    _seed_parent_and_nested_reply(db)
    client.login("alice", "pw12345")
    r = client.get("/tweet/TW1")
    assert r.status_code == 200
    text = r.text
    # Both replies appear
    assert "top reply by A" in text
    assert "nested reply by B" in text
    # The nested reply should appear AFTER its parent in the HTML, and within
    # the indented "ml-6" border-l block that we generated for children.
    parent_idx = text.index("top reply by A")
    nested_idx = text.index("nested reply by B")
    assert parent_idx < nested_idx
    # Find the "ml-6 mt-2 space-y-1 border-l" wrapper between parent and nested.
    between = text[parent_idx:nested_idx]
    assert "ml-6" in between and "border-l" in between, (
        "child reply should be inside an indented wrapper under its parent"
    )


def test_tweet_detail_flat_when_no_parent_reply_ids(client, make_user, db):
    u = make_user("bob", "pw12345")
    with db.cursor() as cur:
        cur.execute("INSERT INTO accounts(username) VALUES('author_y'), ('flat_r') RETURNING id")
        cur.execute("SELECT id FROM accounts WHERE username='author_y'")
        author_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM accounts WHERE username='flat_r'")
        r_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, replies) "
            "VALUES('TW2', %s, NOW(), 'flat post', 1)", (author_id,),
        )
        # Reply with no parent_reply_id at all (legacy)
        cur.execute(
            "INSERT INTO replies(tweet_id, reply_id, author_account_id, content, created_at, likes) "
            "VALUES('TW2', 'R_flat', %s, 'flat reply', NOW(), 1)", (r_id,),
        )
    db.commit()
    client.login("bob", "pw12345")
    r = client.get("/tweet/TW2")
    assert r.status_code == 200
    assert "flat reply" in r.text
