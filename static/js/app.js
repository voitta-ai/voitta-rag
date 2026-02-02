/**
 * voitta-rag - Client-side JavaScript
 */

// ============================================
// Theme Management
// ============================================

function getPreferredTheme() {
    const stored = localStorage.getItem('voitta-theme');
    if (stored) return stored;

    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('voitta-theme', theme);
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    setTheme(next);
}

// Initialize theme on page load
document.addEventListener('DOMContentLoaded', () => {
    setTheme(getPreferredTheme());
});

// ============================================
// WebSocket Connection
// ============================================

let ws = null;
let wsReconnectTimeout = null;

function initWebSocket() {
    if (ws && ws.readyState === WebSocket.OPEN) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('WebSocket connected');
        if (wsReconnectTimeout) {
            clearTimeout(wsReconnectTimeout);
            wsReconnectTimeout = null;
        }
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === 'ping') return;

        handleFileSystemEvent(data);
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected, reconnecting...');
        wsReconnectTimeout = setTimeout(initWebSocket, 3000);
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

function handleFileSystemEvent(event) {
    // Show toast notification
    let message = '';
    switch (event.type) {
        case 'created':
            message = `${event.is_dir ? 'Folder' : 'File'} created: ${event.path}`;
            break;
        case 'deleted':
            message = `${event.is_dir ? 'Folder' : 'File'} deleted: ${event.path}`;
            break;
        case 'modified':
            message = `File modified: ${event.path}`;
            break;
        case 'moved':
            message = `Moved: ${event.path} → ${event.dest_path}`;
            break;
    }

    if (message) {
        showToast(message, 'info');
    }

    // Refresh file list if we're in the affected directory
    if (typeof currentPath !== 'undefined') {
        const eventDir = event.path.split('/').slice(0, -1).join('/');
        if (eventDir === currentPath || event.path.startsWith(currentPath + '/')) {
            refreshFileList();
        }
    }
}

// ============================================
// File Operations
// ============================================

async function refreshFileList() {
    try {
        const response = await fetch(`/api/folders/${currentPath}`);
        if (!response.ok) throw new Error('Failed to fetch folder contents');

        // Reload the page to get updated content
        window.location.reload();
    } catch (error) {
        console.error('Error refreshing file list:', error);
    }
}

async function uploadFiles(files) {
    if (!files || files.length === 0) return;

    for (const file of files) {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('path', currentPath);

        try {
            const response = await fetch('/api/files/upload', {
                method: 'POST',
                body: formData,
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Upload failed');
            }

            showToast(`Uploaded: ${file.name}`, 'success');
        } catch (error) {
            showToast(`Failed to upload ${file.name}: ${error.message}`, 'error');
        }
    }

    // Clear the file input
    document.getElementById('file-upload').value = '';

    // Refresh the file list
    refreshFileList();
}

// ============================================
// Folder Operations
// ============================================

function openCreateFolderModal() {
    document.getElementById('create-folder-modal').classList.add('active');
    document.getElementById('folder-name').focus();
}

function closeCreateFolderModal() {
    document.getElementById('create-folder-modal').classList.remove('active');
    document.getElementById('folder-name').value = '';
}

async function createFolder(event) {
    event.preventDefault();

    const name = document.getElementById('folder-name').value.trim();
    if (!name) return;

    try {
        const response = await fetch('/api/folders', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                name: name,
                path: currentPath,
            }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create folder');
        }

        showToast(`Created folder: ${name}`, 'success');
        closeCreateFolderModal();
        refreshFileList();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

// ============================================
// File Selection & Navigation
// ============================================

let selectedItem = null;
let selectedPath = null;
let selectedIsDir = false;

function selectItem(element, path, isDir) {
    // Remove selection from previous item
    if (selectedItem) {
        selectedItem.classList.remove('selected');
    }

    // Add selection to new item
    element.classList.add('selected');
    selectedItem = element;
    selectedPath = path;
    selectedIsDir = isDir;

    // Fetch and display item details in sidebar
    loadItemDetails(path);

    // If it's a folder, navigate on double-click
    if (isDir) {
        if (element.dataset.lastClick && Date.now() - element.dataset.lastClick < 300) {
            window.location.href = `/browse/${path}`;
        }
        element.dataset.lastClick = Date.now();
    }
}

async function loadItemDetails(path) {
    try {
        const response = await fetch(`/api/details/${encodeURIComponent(path)}`);
        if (!response.ok) throw new Error('Failed to load details');

        const data = await response.json();
        updateSidebar(data);
    } catch (error) {
        console.error('Error loading item details:', error);
    }
}

function updateSidebar(details) {
    // Update header
    const titleEl = document.getElementById('selected-item-title');
    const pathEl = document.getElementById('selected-item-path');
    if (titleEl) titleEl.textContent = details.name;
    if (pathEl) pathEl.textContent = details.path;

    // Show/hide folder settings section
    const folderSettingsSection = document.getElementById('folder-settings-section');
    if (folderSettingsSection) {
        if (details.is_dir) {
            folderSettingsSection.style.display = 'block';

            // Update folder enabled toggle
            const folderEnabledCheckbox = document.getElementById('folder-enabled');
            if (folderEnabledCheckbox) {
                folderEnabledCheckbox.checked = details.folder_enabled || false;
            }

            // Update index status
            const statusValue = document.getElementById('index-status-value');
            if (statusValue) {
                statusValue.className = `index-status-value status-${details.index_status || 'none'}`;
                const statusLabels = {
                    'indexed': 'Indexed',
                    'indexing': 'Indexing...',
                    'pending': 'Pending',
                    'error': 'Error',
                    'none': 'Not indexed'
                };
                statusValue.textContent = statusLabels[details.index_status] || 'Not indexed';
            }
        } else {
            folderSettingsSection.style.display = 'none';
        }
    }

    // Update metadata section
    const metadataContent = document.getElementById('metadata-content');
    const metadataPlaceholder = document.getElementById('metadata-placeholder');
    const metadataText = document.getElementById('metadata-text');
    const metadataInfo = document.getElementById('metadata-info');

    if (metadataContent) metadataContent.style.display = 'block';
    if (metadataPlaceholder) metadataPlaceholder.style.display = 'none';

    if (metadataText) {
        metadataText.value = details.metadata_text || '';
    }

    if (metadataInfo) {
        metadataInfo.textContent = details.metadata_updated_by
            ? `Last updated by ${details.metadata_updated_by}`
            : '';
    }
}

// ============================================
// Metadata
// ============================================

let metadataSaveTimeout = null;

async function saveMetadata(text) {
    // Use selected path or fall back to current path
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No item selected', 'error');
        return;
    }

    // Debounce saves
    if (metadataSaveTimeout) {
        clearTimeout(metadataSaveTimeout);
    }

    metadataSaveTimeout = setTimeout(async () => {
        try {
            const response = await fetch(`/api/metadata/${encodeURIComponent(targetPath)}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ text }),
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Failed to save metadata');
            }

            showToast('Metadata saved', 'success');
        } catch (error) {
            showToast(error.message, 'error');
        }
    }, 500);
}

// ============================================
// Folder Settings
// ============================================

async function toggleFolderEnabled(enabled) {
    // Use selected path or fall back to current path
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        document.getElementById('folder-enabled').checked = !enabled;
        return;
    }

    try {
        const response = await fetch(`/api/settings/folders/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ enabled }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update setting');
        }

        showToast(enabled ? 'Folder enabled for indexing' : 'Folder disabled for indexing', 'success');
    } catch (error) {
        showToast(error.message, 'error');
        // Revert checkbox
        document.getElementById('folder-enabled').checked = !enabled;
    }
}

// ============================================
// Index Operations
// ============================================

async function triggerIndex() {
    try {
        const response = await fetch(`/api/index/${currentPath}`, {
            method: 'POST',
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to trigger index');
        }

        const result = await response.json();
        showToast(result.message, 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

// ============================================
// Toast Notifications
// ============================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;

    container.appendChild(toast);

    // Auto-remove after 4 seconds
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// ============================================
// Keyboard Shortcuts
// ============================================

document.addEventListener('keydown', (event) => {
    // Escape to close modals
    if (event.key === 'Escape') {
        const modal = document.querySelector('.modal.active');
        if (modal) {
            modal.classList.remove('active');
        }
    }
});
