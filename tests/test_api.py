"""API tests."""

import pytest


def test_landing_page(client):
    """Test landing page loads."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Welcome to voitta" in response.text


def test_landing_shows_users(client):
    """Test landing page shows all users."""
    response = client.get("/")
    assert "Roman" in response.text
    assert "Nadya" in response.text
    assert "Greg" in response.text


def test_select_user_sets_cookie(client):
    """Test selecting a user sets the cookie."""
    response = client.post("/select-user/1", follow_redirects=False)
    assert response.status_code == 302
    assert "voitta_user_id" in response.cookies


def test_browse_requires_user(client):
    """Test browse page redirects without user."""
    response = client.get("/browse", follow_redirects=False)
    assert response.status_code == 307


def test_browse_with_user(client):
    """Test browse page works with user."""
    # Select user first
    client.post("/select-user/1")

    response = client.get("/browse")
    assert response.status_code == 200
    assert "voitta" in response.text


def test_create_folder_api(client, temp_root):
    """Test folder creation API."""
    # Select user first
    client.post("/select-user/1")

    response = client.post(
        "/api/folders",
        json={"name": "test-folder", "path": ""},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "test-folder"
    assert data["is_dir"] is True

    # Verify folder exists
    assert (temp_root / "test-folder").exists()


def test_list_folders_api(client, temp_root):
    """Test folder listing API."""
    # Create a test folder
    (temp_root / "my-folder").mkdir()

    # Select user
    client.post("/select-user/1")

    response = client.get("/api/folders")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "my-folder"


def test_metadata_api(client):
    """Test metadata API."""
    # Select user
    client.post("/select-user/1")

    # Create a folder to add metadata to
    client.post("/api/folders", json={"name": "meta-test", "path": ""})

    # Set metadata
    response = client.put(
        "/api/metadata/meta-test",
        json={"text": "This is test metadata"},
    )
    assert response.status_code == 200

    # Get metadata
    response = client.get("/api/metadata/meta-test")
    assert response.status_code == 200
    data = response.json()
    assert data["metadata_text"] == "This is test metadata"


def test_folder_settings_api(client):
    """Test folder settings API."""
    # Select user
    client.post("/select-user/1")

    # Create a folder
    client.post("/api/folders", json={"name": "settings-test", "path": ""})

    # Toggle enabled
    response = client.put(
        "/api/settings/folders/settings-test",
        json={"enabled": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True

    # Get setting
    response = client.get("/api/settings/folders/settings-test")
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True


def test_index_placeholder(client):
    """Test index placeholder API."""
    # Select user
    client.post("/select-user/1")

    # Create a folder
    client.post("/api/folders", json={"name": "index-test", "path": ""})

    # Trigger index
    response = client.post("/api/index/index-test")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"
    assert "placeholder" in data["message"]
