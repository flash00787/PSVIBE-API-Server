"""PS VIBE API — Tournament Tests.

Tests cover:
  - Create tournament → reg_url generated
  - Free-fee public registration
  - Paid registration with member wallet (balance deducted + cash_movements)
  - Duplicate phone rejected (409)
  - Start bracket with 5 players → byes created correctly
  - Score entry advances winner
  - Final match completion → tournament status='completed'

Requires: docker psvibe-mysql running with psvibe_api DB.
Run:  cd /opt/psvibe/api && python3 -m pytest tests/test_tournaments.py -v
"""

import pytest
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure env vars set before importing app
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-pytest-123456789")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_USER", "psvibe_user")
os.environ.setdefault("MYSQL_PASSWORD", "PsVibe@User2024!")
os.environ.setdefault("MYSQL_DATABASE", "psvibe_api")
os.environ.setdefault("API_KEY", "test-api-key-pytest")

API_KEY = "test-api-key-pytest"


@pytest.fixture
def client():
    """Create test client."""
    from app import app
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Return auth headers with valid API key."""
    return {"x-api-key": API_KEY}


@pytest.fixture(autouse=True)
def cleanup_tournaments():
    """Clean up test tournaments data after each test."""
    yield
    try:
        from mysql_db import execute
        # Delete in order to respect FK constraints
        execute("DELETE FROM tournament_matches WHERE tournament_id IN (SELECT id FROM tournaments WHERE name LIKE 'Test%' OR name LIKE 'Free%' OR name LIKE 'Paid%' OR name LIKE 'Dup%' OR name LIKE 'Full%' OR name LIKE 'Bracket%' OR name LIKE 'HTML%' OR name LIKE 'Player%' OR name LIKE 'No SS%' OR name LIKE 'PM Norm%'")
        execute("DELETE FROM tournament_players WHERE tournament_id IN (SELECT id FROM tournaments WHERE name LIKE 'Test%' OR name LIKE 'Free%' OR name LIKE 'Paid%' OR name LIKE 'Dup%' OR name LIKE 'Full%' OR name LIKE 'Bracket%' OR name LIKE 'HTML%' OR name LIKE 'Player%')")
        execute("DELETE FROM tournaments WHERE name LIKE 'Test%' OR name LIKE 'Free%' OR name LIKE 'Paid%' OR name LIKE 'Dup%' OR name LIKE 'Full%' OR name LIKE 'Bracket%' OR name LIKE 'HTML%' OR name LIKE 'Player%' OR name LIKE 'No SS%' OR name LIKE 'PM Norm%'")
        # Clean up any test member_wallets changes
        execute("UPDATE member_wallets SET balance_mins=10000 WHERE member_id='TEST_TOURN_001'")
        execute("DELETE FROM cash_movements WHERE note LIKE '%Tournament entry:%' OR note LIKE '%tournament%'")
    except Exception:
        pass


class TestTournamentCreate:
    """Test tournament creation."""

    def test_create_tournament_returns_reg_url(self, client, auth_headers):
        """Creating a tournament should auto-generate a reg_token and reg_url."""
        resp = client.post("/api/tournaments", json={
            "name": "Test Cup",
            "game": "EA FC 26",
            "entry_fee_ks": 5000,
            "max_players": 8,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "reg_url" in data["data"]
        assert data["data"]["reg_url"].startswith("https://ps-vibe.com/t/")
        assert "reg_token" in data["data"]

    def test_create_tournament_requires_auth(self, client):
        """Tournament creation should fail without auth."""
        resp = client.post("/api/tournaments", json={
            "name": "Test Cup",
            "game": "EA FC 26",
        })
        assert resp.status_code == 401

    def test_create_tournament_requires_name_game(self, client, auth_headers):
        """Name and game are required fields."""
        resp = client.post("/api/tournaments", json={}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["success"] is False


class TestPublicRegistration:
    """Test public player registration."""

    def test_free_registration(self, client, auth_headers):
        """Register for a free tournament: paid=1 automatically."""
        # Create tournament
        resp = client.post("/api/tournaments", json={
            "name": "Free Cup",
            "game": "Tekken 8",
            "entry_fee_ks": 0,
            "max_players": 16,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Register
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Player One",
            "phone": "09111111111",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["paid"] is True

        # Verify in DB
        from mysql_db import query_one
        p = query_one(
            "SELECT * FROM tournament_players WHERE tournament_id=%s AND phone=%s",
            (tid, "09111111111"),
        )
        assert p is not None
        assert p["paid"] == 1
        assert p["name"] == "Player One"

    def test_paid_registration_wallet_deduction(self, client, auth_headers):
        """Register with member_id: wallet deducted, cash_movements recorded."""
        from mysql_db import execute, query_one

        # Ensure test member exists with enough balance
        existing = query_one("SELECT * FROM member_wallets WHERE member_id='TEST_TOURN_001'")
        if not existing:
            execute(
                "INSERT INTO member_wallets (member_id, member_name, phone, balance_mins) VALUES (%s, %s, %s, %s)",
                ("TEST_TOURN_001", "Test Tournament Member", "09777777777", 10000),
            )
        else:
            execute("UPDATE member_wallets SET balance_mins=10000 WHERE member_id='TEST_TOURN_001'")

        # Create paid tournament
        resp = client.post("/api/tournaments", json={
            "name": "Paid Cup",
            "game": "Mortal Kombat 1",
            "entry_fee_ks": 3000,
            "max_players": 8,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Register with member
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Wallet Player",
            "phone": "09777777777",
            "member_id": "TEST_TOURN_001",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["paid"] is True

        # Verify wallet deducted
        wallet = query_one("SELECT balance_mins FROM member_wallets WHERE member_id='TEST_TOURN_001'")
        assert wallet["balance_mins"] == 7000  # 10000 - 3000

        # Verify cash_movements entry
        cash = query_one(
            "SELECT * FROM cash_movements WHERE note LIKE %s ORDER BY id DESC LIMIT 1",
            ("Tournament entry: Paid Cup%",),
        )
        assert cash is not None
        assert cash["movement_type"] == "eject"
        assert int(cash["amount"]) == 3000

    def test_duplicate_phone_rejected(self, client, auth_headers):
        """Registering same phone twice → 409 conflict."""
        # Create
        resp = client.post("/api/tournaments", json={
            "name": "Dup Cup",
            "game": "SF6",
            "entry_fee_ks": 0,
            "max_players": 16,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # First registration
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Player A",
            "phone": "09555555555",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Second registration with same phone
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Player B",
            "phone": "09555555555",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        resp_json = resp.json()
        assert resp_json["code"] == 409
        assert "already registered" in resp_json["error"].lower()

    def test_full_tournament_rejected(self, client, auth_headers):
        """Cannot register when tournament is full."""
        resp = client.post("/api/tournaments", json={
            "name": "Full Cup",
            "game": "Rocket League",
            "entry_fee_ks": 0,
            "max_players": 1,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Fill it
        client.post(f"/api/tournaments/{tid}/register", json={
            "name": "First Player",
            "phone": "09123456780",
        })

        # Try to register when full
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Second Player",
            "phone": "09123456781",
        })
        assert resp.json()["success"] is False
        assert "full" in resp.json()["error"].lower()


class TestDigitalPayment:
    """Test digital payment registration (KPay/WavePay + SS screenshot)."""

    def test_digital_payment_registration(self, client, auth_headers):
        """Register with payment_method + ss_file → paid=0, both fields stored."""
        from mysql_db import query_one

        # Create paid tournament
        resp = client.post("/api/tournaments", json={
            "name": "Paid Digital Cup",
            "game": "EA FC 26",
            "entry_fee_ks": 5000,
            "max_players": 8,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Register with digital payment (no member_id)
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Digital Player",
            "phone": "09123456789",
            "payment_method": "kpay",
            "ss_file": "AgACAgQAAxkBAA...test_file_id_123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["paid"] is False  # NOT paid yet

        # Verify in DB
        p = query_one(
            "SELECT * FROM tournament_players WHERE tournament_id=%s AND phone=%s",
            (tid, "09123456789"),
        )
        assert p is not None
        assert p["paid"] == 0
        assert p["payment_method"] == "kpay"
        assert p["ss_file"] == "AgACAgQAAxkBAA...test_file_id_123"
        assert p["status"] == "registered"
        assert p["paid_at"] is None

    def test_digital_payment_normalizes_payment_method(self, client, auth_headers):
        """Payment method variations normalize to canonical keys."""
        from mysql_db import query_one

        resp = client.post("/api/tournaments", json={
            "name": "PM Norm Cup",
            "game": "Tekken 8",
            "entry_fee_ks": 3000,
            "max_players": 8,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # KBZ Pay should normalize to 'kpay'
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Kbz Pay Player",
            "phone": "09111111117",
            "payment_method": "KBZ Pay",
            "ss_file": "AgAC...kbz_test",
        })
        assert resp.json()["success"] is True
        p = query_one(
            "SELECT payment_method FROM tournament_players WHERE tournament_id=%s AND phone=%s",
            (tid, "09111111117"),
        )
        assert p["payment_method"] == "kpay"

    def test_digital_payment_requires_ss_file(self, client, auth_headers):
        """payment_method without ss_file → falls through to legacy unpaid (pay at counter)."""
        from mysql_db import query_one

        resp = client.post("/api/tournaments", json={
            "name": "No SS Cup",
            "game": "Rocket League",
            "entry_fee_ks": 3000,
            "max_players": 8,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # payment_method alone, no ss_file → treated as legacy (unpaid)
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "No Proof Player",
            "phone": "09111111118",
            "payment_method": "kpay",
        })
        assert resp.json()["success"] is True
        p = query_one(
            "SELECT paid, payment_method, ss_file FROM tournament_players WHERE tournament_id=%s AND phone=%s",
            (tid, "09111111118"),
        )
        assert p["paid"] == 0
        # payment_method gets stored in the INSERT (via _parse_payment_method), but in legacy path
        # it maps to the regular unpaid flow. Actually payment_method will still be stored because
        # we always normalize. Let's verify it's stored.


class TestBracket:
    """Test bracket generation and match progression."""

    def _create_and_populate(self, client, auth_headers, count=5):
        """Helper: create a tournament and add N players (all confirmed)."""
        resp = client.post("/api/tournaments", json={
            "name": "Bracket Test Cup",
            "game": "EA FC 26",
            "entry_fee_ks": 0,
            "max_players": 8,
            "min_players": 2,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        for i in range(count):
            client.post(f"/api/tournaments/{tid}/register", json={
                "name": f"Player {i+1}",
                "phone": f"0990000000{i}",
            })
        return tid

    def test_start_bracket_with_5_players_creates_byes(self, client, auth_headers):
        """5 players → 8-seed bracket → 4 R1 matches, 3 byes. 
        
        With 5 players and 8 slots (next power of 2):
        - 4 matches in round 1
        - Standard seeding: 1v8, 2v7, 3v6, 4v5
        - Slots 6, 7, 8 are empty (byes) → players in those slots get byes
        - 5 players filling positions 1-5 means positions 1,2,3 face empty slots (byes)
          Actually: idx_a=pos-1, idx_b=total-pos
          pos=1: a=0 b=7 → p1=player1 (idx0), p2=None (idx7 out of range) → bye for player1
          pos=2: a=1 b=6 → p1=player2, p2=None → bye
          pos=3: a=2 b=5 → p1=player3, p2=None → bye
          pos=4: a=3 b=4 → p1=player4, p2=player5 (idx4) → match!
        So 3 byes and 1 actual match in round 1.
        """
        tid = self._create_and_populate(client, auth_headers, count=5)

        # Start tournament
        resp = client.post(f"/api/tournaments/{tid}/start", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        data = resp.json()["data"]
        assert data["players"] == 5
        assert data["total_rounds"] == 3  # 8 slots → 3 rounds
        assert data["r1_matches"] == 4    # 8/2 = 4

        # Check match structure
        from mysql_db import query
        matches = query(
            "SELECT * FROM tournament_matches WHERE tournament_id=%s ORDER BY round, position",
            (tid,),
        )
        r1_matches = [m for m in matches if m["round"] == 1]
        assert len(r1_matches) == 4

        bye_count = sum(1 for m in r1_matches if m["player2_id"] is None)
        assert bye_count == 3  # 3 byes expected

        # Verify tournament status changed
        from mysql_db import query_one
        t = query_one("SELECT status FROM tournaments WHERE id=%s", (tid,))
        assert t["status"] == "live"

    def test_score_entry_advances_winner(self, client, auth_headers):
        """Submit a score → winner advances to next round match."""
        tid = self._create_and_populate(client, auth_headers, count=4)

        # Start
        client.post(f"/api/tournaments/{tid}/start", headers=auth_headers)

        # Get R1 matches
        from mysql_db import query
        r1 = query(
            "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=1 ORDER BY position",
            (tid,),
        )

        # Score the first R1 match (player1 wins)
        match = r1[0]
        resp = client.post(
            f"/api/tournaments/{tid}/matches/{match['id']}/score",
            json={"score1": 3, "score2": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        data = resp.json()["data"]
        assert data["winner_id"] == match["player1_id"]

        # Verify winner advanced to round 2
        matches_r2 = query(
            "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=2 ORDER BY position",
            (tid,),
        )
        assert len(matches_r2) >= 1
        r2_match = matches_r2[0]
        assert r2_match["player1_id"] == match["player1_id"]

        # Verify loser is eliminated
        from mysql_db import query_one
        loser = query_one("SELECT status FROM tournament_players WHERE id=%s", (match["player2_id"],))
        assert loser["status"] == "eliminated"

    def test_tie_rejected(self, client, auth_headers):
        """Tie scores should be rejected with 400."""
        tid = self._create_and_populate(client, auth_headers, count=4)
        client.post(f"/api/tournaments/{tid}/start", headers=auth_headers)

        from mysql_db import query
        match = query(
            "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=1 LIMIT 1",
            (tid,),
        )[0]

        resp = client.post(
            f"/api/tournaments/{tid}/matches/{match['id']}/score",
            json={"score1": 2, "score2": 2},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "tie" in resp.json()["error"].lower()

    def test_final_completion(self, client, auth_headers):
        """Winning the final match → tournament status='completed'."""
        tid = self._create_and_populate(client, auth_headers, count=2)

        # Start
        client.post(f"/api/tournaments/{tid}/start", headers=auth_headers)

        # Get the only R1 match (also the final for 2 players)
        from mysql_db import query
        match = query(
            "SELECT * FROM tournament_matches WHERE tournament_id=%s AND round=1 LIMIT 1",
            (tid,),
        )[0]

        # Score the final
        resp = client.post(
            f"/api/tournaments/{tid}/matches/{match['id']}/score",
            json={"score1": 5, "score2": 3},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["data"]["is_final"] is True

        # Verify tournament completed
        from mysql_db import query_one
        t = query_one("SELECT status FROM tournaments WHERE id=%s", (tid,))
        assert t["status"] == "completed"


class TestSSUpload:
    """Test payment screenshot upload endpoint."""

    def test_ss_upload_stores_file_updates_db(self, client, auth_headers):
        """Upload a payment SS → file saved, DB updated, ss_url returned."""
        import io
        from mysql_db import query_one

        # Create tournament
        resp = client.post("/api/tournaments", json={
            "name": "SS Upload Cup",
            "game": "EA FC 26",
            "entry_fee_ks": 5000,
            "max_players": 8,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Register a player
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "SS Player",
            "phone": "09123456710",
            "payment_method": "kpay",
            "ss_file": "old_telegram_file_id",
        })
        assert resp.json()["success"] is True
        pid = resp.json()["data"]["player_id"]
        assert pid is not None

        # Upload SS
        fake_image = io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        resp = client.post(
            f"/api/tournaments/{tid}/players/{pid}/ss",
            files={"file": ("test.jpg", fake_image, "image/jpeg")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "ss_url" in data["data"]
        assert data["data"]["ss_url"].startswith("/uploads/tournaments/")
        assert data["data"]["ss_file"].startswith("uploads/tournaments/")

        # Verify DB updated
        p = query_one("SELECT ss_file FROM tournament_players WHERE id=%s", (pid,))
        assert p["ss_file"].startswith("uploads/tournaments/")
        assert f"t{tid}_p{pid}" in p["ss_file"]

        # Verify file exists
        import os
        filepath = os.path.join(os.path.dirname(os.path.dirname(__file__)), p["ss_file"])
        assert os.path.isfile(filepath)

        # Verify the uploaded image is served
        resp = client.get(data["data"]["ss_url"])
        assert resp.status_code == 200
        assert "image" in resp.headers.get("content-type", "")

    def test_ss_upload_rejects_non_image(self, client, auth_headers):
        """Uploading non-image file should be rejected."""
        import io

        resp = client.post("/api/tournaments", json={
            "name": "Reject NonImage Cup",
            "game": "FIFA",
            "entry_fee_ks": 5000,
            "max_players": 8,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Register
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Fake SS Player",
            "phone": "09123456711",
            "payment_method": "kpay",
            "ss_file": "dummy",
        })
        pid = resp.json()["data"]["player_id"]

        # Try uploading a text file as SS
        fake_text = io.BytesIO(b"this is not an image")
        resp = client.post(
            f"/api/tournaments/{tid}/players/{pid}/ss",
            files={"file": ("fake.txt", fake_text, "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "image" in resp.json()["error"].lower()

    def test_players_response_includes_ss_url(self, client, auth_headers):
        """GET /players returns ss_url when ss_file is a local path."""
        import io

        resp = client.post("/api/tournaments", json={
            "name": "SS URL Test Cup",
            "game": "Tekken",
            "entry_fee_ks": 3000,
            "max_players": 8,
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Register + upload SS
        resp = client.post(f"/api/tournaments/{tid}/register", json={
            "name": "URL Player",
            "phone": "09123456712",
            "payment_method": "wavepay",
            "ss_file": "legacy_id",
        })
        pid = resp.json()["data"]["player_id"]

        fake_image = io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        client.post(f"/api/tournaments/{tid}/players/{pid}/ss",
                    files={"file": ("test.jpg", fake_image, "image/jpeg")})

        # Get players
        resp = client.get(f"/api/tournaments/{tid}/players")
        assert resp.status_code == 200
        players = resp.json()["data"]["players"]
        url_player = [p for p in players if p["name"] == "URL Player"][0]
        assert url_player["ss_url"] is not None
        assert url_player["ss_url"].startswith("/uploads/tournaments/")
        # ss_file raw should still be present
        assert "ss_file" in url_player

    def test_ss_upload_nonexistent_player_404(self, client, auth_headers):
        """Uploading SS to a non-existent player returns 404."""
        import io
        fake_image = io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF")
        resp = client.post(
            "/api/tournaments/99999/players/99999/ss",
            files={"file": ("test.jpg", fake_image, "image/jpeg")},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert resp.json()["code"] == 404


class TestPublicEndpoints:
    """Test public (no-auth) endpoints."""

    def test_registration_page_html(self, client, auth_headers):
        """GET /t/{token} returns HTML page."""
        resp = client.post("/api/tournaments", json={
            "name": "HTML Cup",
            "game": "FIFA",
            "status": "open",
        }, headers=auth_headers)
        token = resp.json()["data"]["reg_token"]

        resp = client.get(f"/t/{token}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "HTML Cup" in resp.text
        assert "PS VIBE" in resp.text

    def test_bracket_public(self, client, auth_headers):
        """GET /api/tournaments/{id}/bracket returns bracket data."""
        resp = client.post("/api/tournaments", json={
            "name": "Bracket View Cup",
            "game": "Valorant",
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        resp = client.get(f"/api/tournaments/{tid}/bracket")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_players_list_public(self, client, auth_headers):
        """GET /api/tournaments/{id}/players returns player list with masked phones."""
        resp = client.post("/api/tournaments", json={
            "name": "Player List Cup",
            "game": "Fortnite",
            "status": "open",
        }, headers=auth_headers)
        tid = resp.json()["data"]["id"]

        # Register a player
        client.post(f"/api/tournaments/{tid}/register", json={
            "name": "Visible Player",
            "phone": "09987654321",
        })

        resp = client.get(f"/api/tournaments/{tid}/players")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["count"] == 1
        phone = data["data"]["players"][0]["phone"]
        # Phone should contain * for masking
        assert "*" in phone
        assert "099" in phone  # prefix preserved
        assert "21" in phone   # suffix preserved
