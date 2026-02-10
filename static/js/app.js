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

// Initialize theme on page load + hide spinner
document.addEventListener('DOMContentLoaded', () => {
    setTheme(getPreferredTheme());
    hideSpinner();
});

// Show spinner on page unload (navigation / reload)
window.addEventListener('beforeunload', () => {
    showSpinner();
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

        // Route to appropriate handler based on event type
        switch (data.type) {
            case 'sync_status':
                handleSyncStatusEvent(data);
                break;
            case 'index_status':
                handleIndexStatusEvent(data);
                break;
            case 'index_complete':
                handleIndexCompleteEvent(data);
                break;
            case 'sp_connected':
                handleSpConnectedEvent(data);
                break;
            case 'ado_connected':
                handleAdoConnectedEvent(data);
                break;
            case 'box_connected':
                handleBoxConnectedEvent(data);
                break;
            case 'gd_connected':
                handleGdConnectedEvent(data);
                break;
            default:
                // Filesystem events: created, deleted, modified, moved
                handleFileSystemEvent(data);
                break;
        }
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected, reconnecting...');
        wsReconnectTimeout = setTimeout(initWebSocket, 3000);
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

// ============================================
// WebSocket Event Handlers
// ============================================

function handleFileSystemEvent(event) {
    // Suppress notifications for files inside syncing or deleting folders
    const inBusyFolder = [...syncingFolders, ...deletingFolders].some(
        folder => event.path === folder || event.path.startsWith(folder + '/')
    );

    if (!inBusyFolder) {
        // Silently refresh — no per-file toasts
    }

    // Refresh file list if we're in the affected directory (skip during bulk ops)
    if (!inBusyFolder && typeof currentPath !== 'undefined') {
        const eventDir = event.path.split('/').slice(0, -1).join('/');
        if (eventDir === currentPath || event.path.startsWith(currentPath + '/')) {
            refreshFileList();
        }
    }
}

function handleSyncStatusEvent(event) {
    // event: { type: 'sync_status', path, sync_status, sync_error, last_synced_at }
    const folderPath = event.path;

    // Track syncing folders so file-level notifications are suppressed
    if (event.sync_status === 'syncing') {
        syncingFolders.add(folderPath);
    }

    // Update sync status displays if this folder is currently selected/viewed
    if (selectedPath === folderPath || currentPath === folderPath) {
        updateSyncStatusDisplay({
            ...currentSyncSource,
            sync_status: event.sync_status,
            sync_error: event.sync_error,
            last_synced_at: event.last_synced_at,
        });
        updateFolderSyncStatus(event.sync_status, event.last_synced_at);
    }

    // If sync finished, stop tracking and refresh file list
    if (event.sync_status !== 'syncing') {
        syncingFolders.delete(folderPath);
        if (typeof currentPath !== 'undefined') {
            // Refresh if the synced folder is visible
            if (folderPath === currentPath || folderPath.startsWith(currentPath + '/') ||
                currentPath.startsWith(folderPath + '/')) {
                refreshFileList();
            }
        }
        // Status is already reflected in the sidebar UI — no toast needed
    }
}

function handleIndexStatusEvent(event) {
    // event: { type: 'index_status', path, status }
    const folderPath = event.path;
    const status = event.status;

    // Update file list badge
    updateFileListIndexStatus(folderPath, status);

    // Update sidebar if this folder is selected
    if (selectedPath === folderPath || currentPath === folderPath) {
        const statusValue = document.getElementById('index-status-value');
        if (statusValue) {
            statusValue.className = `index-status-value status-${status}`;
            const statusLabels = {
                'indexed': 'Indexed',
                'indexing': 'Indexing...',
                'pending': 'Pending',
                'error': 'Error',
                'none': 'Not indexed'
            };
            statusValue.textContent = statusLabels[status] || 'Not indexed';
        }
    }
}

function handleIndexCompleteEvent(event) {
    // event: { type: 'index_complete', path, files_indexed, total_chunks }
    const folderPath = event.path;

    // Update file list badge to 'indexed'
    updateFileListIndexStatus(folderPath, 'indexed');

    // Update sidebar if this folder is selected
    if (selectedPath === folderPath || currentPath === folderPath) {
        const statusValue = document.getElementById('index-status-value');
        if (statusValue) {
            statusValue.className = 'index-status-value status-indexed';
            statusValue.textContent = 'Indexed';
        }
        // Reload sidebar details to get updated stats
        loadItemDetails(folderPath, true);
    }

    // Refresh file list to update stats columns
    if (typeof currentPath !== 'undefined') {
        if (folderPath === currentPath || folderPath.startsWith(currentPath + '/') ||
            currentPath.startsWith(folderPath + '/')) {
            refreshFileList();
        }
    }

}

function handleSpConnectedEvent(event) {
    // event: { type: 'sp_connected', path }
    const folderPath = event.path;

    // Update UI if we're looking at this folder
    if (selectedPath === folderPath || currentPath === folderPath) {
        updateSpConnectStatus(true);
        // Reload sync source to get updated data
        loadSyncSource(folderPath);
        showToast('SharePoint connected successfully', 'success');
    }
}

function handleAdoConnectedEvent(event) {
    // event: { type: 'ado_connected', path }
    const folderPath = event.path;

    if (selectedPath === folderPath || currentPath === folderPath) {
        updateAdoConnectStatus(true);
        loadSyncSource(folderPath);
        showToast('Azure DevOps connected successfully', 'success');
    }
}

function handleBoxConnectedEvent(event) {
    // event: { type: 'box_connected', path }
    const folderPath = event.path;

    if (selectedPath === folderPath || currentPath === folderPath) {
        updateBoxConnectStatus(true);
        loadSyncSource(folderPath);
        showToast('Box connected successfully', 'success');
    }
}

function handleGdConnectedEvent(event) {
    // event: { type: 'gd_connected', path }
    const folderPath = event.path;

    if (selectedPath === folderPath || currentPath === folderPath) {
        updateGdConnectStatus(true);
        loadSyncSource(folderPath);
        fetchGoogleDriveFolders();
        showToast('Google Drive connected successfully', 'success');
    }
}

// ============================================
// File Operations
// ============================================

let refreshDebounceTimeout = null;

async function refreshFileList() {
    // Debounce rapid refreshes (e.g. multiple file events in quick succession)
    if (refreshDebounceTimeout) {
        clearTimeout(refreshDebounceTimeout);
    }

    refreshDebounceTimeout = setTimeout(async () => {
        refreshDebounceTimeout = null;
        try {
            const url = currentPath
                ? `/api/browse-list/${encodeURIComponent(currentPath)}`
                : '/api/browse-list';
            const response = await fetch(url);
            if (!response.ok) throw new Error('Failed to fetch file list');

            const html = await response.text();
            const fileList = document.getElementById('file-list');
            if (!fileList) return;

            // Remember current selection
            const prevSelectedPath = selectedPath;

            // Replace file list content
            fileList.innerHTML = html;

            // Restore selection if the item still exists
            if (prevSelectedPath) {
                const item = fileList.querySelector(`.file-item[data-path="${CSS.escape(prevSelectedPath)}"]`);
                if (item) {
                    item.classList.add('selected');
                    selectedItem = item;
                } else {
                    // Item was deleted or moved — clear selection
                    selectedItem = null;
                    selectedPath = null;
                    selectedIsDir = false;
                }
            }
        } catch (error) {
            console.error('Error refreshing file list:', error);
        }
    }, 300);
}

async function uploadFiles(files) {
    if (!files || files.length === 0) return;

    const uploadErrors = [];
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

            // upload ok
        } catch (error) {
            uploadErrors.push(file.name);
        }
    }

    // Show a single summary toast
    if (uploadErrors.length > 0) {
        showToast(`Upload: ${files.length - uploadErrors.length} ok, ${uploadErrors.length} failed`, 'error');
    } else if (files.length > 1) {
        showToast(`Uploaded ${files.length} files`, 'success');
    }

    // Clear the file input
    document.getElementById('file-upload').value = '';

    // File watcher will trigger refresh via WebSocket, but refresh immediately too
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
        // File watcher will trigger refresh via WebSocket
    } catch (error) {
        showToast(error.message, 'error');
    }
}

// ============================================
// Delete Folder
// ============================================

let deleteFolderTargetPath = null;

function openDeleteFolderModal() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }
    deleteFolderTargetPath = targetPath;
    const folderName = targetPath.split('/').pop();
    document.getElementById('delete-folder-target-name').textContent = folderName;
    document.getElementById('delete-folder-confirm-name').value = '';
    document.getElementById('delete-folder-modal').classList.add('active');
    document.getElementById('delete-folder-confirm-name').focus();
}

function closeDeleteFolderModal() {
    document.getElementById('delete-folder-modal').classList.remove('active');
    document.getElementById('delete-folder-confirm-name').value = '';
    deleteFolderTargetPath = null;
}

async function confirmDeleteFolder(event) {
    event.preventDefault();

    const confirmName = document.getElementById('delete-folder-confirm-name').value.trim();
    const folderName = deleteFolderTargetPath.split('/').pop();

    if (confirmName.toLowerCase() !== folderName.toLowerCase()) {
        showToast('Folder name does not match', 'error');
        return;
    }

    // Show spinner state
    const btn = document.getElementById('btn-confirm-delete');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Deleting...';

    // Suppress file-level notifications during deletion
    deletingFolders.add(deleteFolderTargetPath);

    try {
        const response = await fetch(`/api/folders/${encodeURIComponent(deleteFolderTargetPath)}`, {
            method: 'DELETE',
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete folder');
        }

        showToast(`Deleted folder: ${folderName}`, 'success');

        // Compute parent before closing modal (which nulls deleteFolderTargetPath)
        const parentPath = deleteFolderTargetPath.split('/').slice(0, -1).join('/');
        closeDeleteFolderModal();
        window.location.href = parentPath ? `/browse/${parentPath}` : '/browse';
    } catch (error) {
        showToast(error.message, 'error');
        btn.disabled = false;
        btn.textContent = originalText;
        deletingFolders.delete(deleteFolderTargetPath);
    }
}

// ============================================
// File Selection & Navigation
// ============================================

let selectedItem = null;
let selectedPath = null;
let selectedIsDir = false;

function selectItem(element, path, isDir) {
    const alreadySelected = selectedPath === path;

    // Remove selection from previous item
    if (selectedItem) {
        selectedItem.classList.remove('selected');
    }

    // Add selection to new item
    element.classList.add('selected');
    selectedItem = element;
    selectedPath = path;
    selectedIsDir = isDir;

    // Fetch details only if selecting a new item
    if (!alreadySelected) {
        loadItemDetails(path, isDir);
    }

    // If it's a folder, navigate on double-click
    if (isDir) {
        if (element.dataset.lastClick && Date.now() - element.dataset.lastClick < 300) {
            window.location.href = `/browse/${path}`;
        }
        element.dataset.lastClick = Date.now();
    }
}

async function loadItemDetails(path, isDir = true) {
    if (isDir) showSpinner(300);
    try {
        const response = await fetch(`/api/details/${encodeURIComponent(path)}`);
        if (!response.ok) throw new Error('Failed to load details');

        const data = await response.json();
        updateSidebar(data);
    } catch (error) {
        console.error('Error loading item details:', error);
    } finally {
        hideSpinner();
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
    const fileIndexSection = document.getElementById('file-index-section');

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

    // Show/hide file index section
    if (fileIndexSection) {
        if (!details.is_dir) {
            fileIndexSection.style.display = 'block';

            const fileStatusValue = document.getElementById('file-index-status-value');
            const fileChunkCount = document.getElementById('file-chunk-count');
            const fileIndexedAt = document.getElementById('file-indexed-at');

            if (fileStatusValue) {
                fileStatusValue.className = `index-status-value status-${details.index_status || 'none'}`;
                fileStatusValue.textContent = details.index_status === 'indexed' ? 'Indexed' : 'Not indexed';
            }
            if (fileChunkCount) {
                fileChunkCount.textContent = details.chunk_count ? `${details.chunk_count} chunks` : '—';
            }
            if (fileIndexedAt) {
                if (details.indexed_at) {
                    const date = new Date(details.indexed_at);
                    fileIndexedAt.textContent = date.toLocaleString();
                } else {
                    fileIndexedAt.textContent = '—';
                }
            }
        } else {
            fileIndexSection.style.display = 'none';
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

    // Update folder sync status in properties card
    updateFolderSyncStatus(details.sync_status, details.last_synced_at);

    // Show/hide sync source section
    currentFolderIsEmpty = details.is_empty;
    const syncSection = document.getElementById('sync-source-section');
    if (syncSection) {
        if (details.is_dir && (details.sync_source_type || details.is_empty)) {
            syncSection.style.display = 'block';
            loadSyncSource(details.path);
        } else {
            syncSection.style.display = 'none';
            clearSyncFields();
        }
    }

    // Update indexing stats section (folders only)
    const indexingStatsSection = document.getElementById('indexing-stats-section');
    const indexingStatsBody = document.getElementById('indexing-stats-body');

    if (indexingStatsSection && indexingStatsBody) {
        if (details.is_dir && details.file_type_stats && details.file_type_stats.length > 0) {
            indexingStatsSection.style.display = 'block';

            // Build table rows
            let html = '';
            for (const stat of details.file_type_stats) {
                html += `<tr>
                    <td class="stats-ext">${escapeHtml(stat.extension)}</td>
                    <td class="stats-num">${stat.total_count}</td>
                    <td class="stats-num">${stat.indexed_count}</td>
                    <td class="stats-num">${stat.chunk_count}</td>
                </tr>`;
            }
            indexingStatsBody.innerHTML = html;
        } else {
            indexingStatsSection.style.display = 'none';
            indexingStatsBody.innerHTML = '';
        }
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

        // Refresh sidebar to show updated index status
        await loadItemDetails(targetPath);

        // Update file list tag for this folder
        updateFileListIndexStatus(targetPath, enabled ? 'pending' : 'none');
    } catch (error) {
        showToast(error.message, 'error');
        // Revert checkbox
        document.getElementById('folder-enabled').checked = !enabled;
    }
}

async function toggleSearchActive(active) {
    // Use selected path or fall back to current path
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        document.getElementById('folder-search-active').checked = !active;
        return;
    }

    try {
        const response = await fetch(`/api/settings/folders/${encodeURIComponent(targetPath)}/search-active`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ search_active: active }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update setting');
        }

        showToast(active ? 'Folder activated for search' : 'Folder deactivated for search', 'success');
    } catch (error) {
        showToast(error.message, 'error');
        // Revert checkbox
        document.getElementById('folder-search-active').checked = !active;
    }
}

async function toggleSearchActiveInline(checkbox, folderPath) {
    const active = checkbox.checked;
    const fileItem = checkbox.closest('.file-item');

    try {
        const response = await fetch(`/api/settings/folders/${encodeURIComponent(folderPath)}/search-active`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ search_active: active }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update setting');
        }

        // Update data attribute
        if (fileItem) {
            fileItem.dataset.searchActive = active ? 'true' : 'false';
        }

        showToast(active ? 'Folder activated for search' : 'Folder deactivated for search', 'success');
    } catch (error) {
        showToast(error.message, 'error');
        // Revert checkbox
        checkbox.checked = !active;
    }
}

async function reindexFolder() {
    // Use selected path or fall back to current path
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    try {
        const response = await fetch(`/api/settings/folders/${encodeURIComponent(targetPath)}/reindex`, {
            method: 'POST',
        });

        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.detail || 'Failed to trigger re-index');
        }

        showToast(result.message, 'success');

        // Refresh sidebar to show updated status
        await loadItemDetails(targetPath);

        // Update file list tag for this folder
        updateFileListIndexStatus(targetPath, result.status);
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function updateFileListIndexStatus(path, status) {
    // Find the file item in the list and update its status tag
    const fileItem = document.querySelector(`.file-item[data-path="${CSS.escape(path)}"]`);
    if (!fileItem) return;

    fileItem.dataset.indexStatus = status;
    const statusTag = fileItem.querySelector('.index-status-tag');
    if (!statusTag) return;

    // Update class
    statusTag.className = `index-status-tag status-${status}`;

    // Update content
    const statusLabels = {
        'indexed': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon"><polyline points="20 6 9 17 4 12"></polyline></svg> Indexed`,
        'indexing': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon spin"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"></path></svg> Indexing`,
        'pending': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg> Pending`,
        'error': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg> Error`,
        'none': 'Not indexed'
    };

    statusTag.innerHTML = statusLabels[status] || 'Not indexed';
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
// Loading Spinner
// ============================================

let spinnerTimeout = null;

function showSpinner(delay = 0) {
    clearSpinnerTimeout();
    if (delay > 0) {
        spinnerTimeout = setTimeout(() => {
            const overlay = document.getElementById('spinner-overlay');
            if (overlay) overlay.classList.add('active');
        }, delay);
    } else {
        const overlay = document.getElementById('spinner-overlay');
        if (overlay) overlay.classList.add('active');
    }
}

function hideSpinner() {
    clearSpinnerTimeout();
    const overlay = document.getElementById('spinner-overlay');
    if (overlay) overlay.classList.remove('active');
}

function clearSpinnerTimeout() {
    if (spinnerTimeout) {
        clearTimeout(spinnerTimeout);
        spinnerTimeout = null;
    }
}

// ============================================
// Utility Functions
// ============================================

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============================================
// Remote Sync
// ============================================

let currentSyncSource = null;
let syncingFolders = new Set();
let deletingFolders = new Set();
let currentFolderIsEmpty = true;

function onSyncSourceTypeChange(value) {
    document.querySelectorAll('.sync-fields').forEach(el => el.style.display = 'none');

    if (value) {
        const fields = document.getElementById(`sync-fields-${value}`);
        if (fields) fields.style.display = 'block';
        document.getElementById('sync-actions').style.display = 'flex';
    } else {
        document.getElementById('sync-actions').style.display =
            currentSyncSource ? 'flex' : 'none';
    }
}

function populateSyncFields(data) {
    currentSyncSource = data;

    // Track syncing folders so file notifications are suppressed during sync
    if (data.sync_status === 'syncing' && data.folder_path) {
        syncingFolders.add(data.folder_path);
    }

    const locked = data.source_type && !currentFolderIsEmpty;

    const select = document.getElementById('sync-source-type');
    select.value = data.source_type || '';
    select.disabled = locked;
    onSyncSourceTypeChange(data.source_type || '');

    if (data.source_type === 'sharepoint' && data.sharepoint) {
        document.getElementById('sp-tenant-id').value = data.sharepoint.tenant_id || '';
        document.getElementById('sp-client-id').value = data.sharepoint.client_id || '';
        document.getElementById('sp-client-secret').value = data.sharepoint.client_secret || '';
        document.getElementById('sp-site-url').value = data.sharepoint.site_url || '';
        document.getElementById('sp-drive-id').value = data.sharepoint.drive_id || '';
        updateSpConnectStatus(data.sharepoint.connected);
    } else if (data.source_type === 'google_drive' && data.google_drive) {
        document.getElementById('gd-client-id').value = data.google_drive.client_id || '';
        document.getElementById('gd-client-secret').value = data.google_drive.client_secret || '';
        updateGdConnectStatus(data.google_drive.connected);
        if (data.google_drive.connected) {
            fetchGoogleDriveFolders(data.google_drive.folder_id || '');
        } else {
            const folderSelect = document.getElementById('gd-folder-id');
            folderSelect.innerHTML = '<option value="">Select a folder...</option>';
            if (data.google_drive.folder_id) {
                const opt = document.createElement('option');
                opt.value = data.google_drive.folder_id;
                opt.textContent = data.google_drive.folder_id;
                folderSelect.appendChild(opt);
                folderSelect.value = data.google_drive.folder_id;
            }
        }
    } else if (data.source_type === 'github' && data.github) {
        document.getElementById('gh-repo').value = data.github.repo || '';
        document.getElementById('gh-path').value = data.github.path || '';
        document.getElementById('gh-auth-method').value = data.github.auth_method || 'ssh';
        document.getElementById('gh-ssh-key').value = data.github.ssh_key || '';
        document.getElementById('gh-username').value = data.github.username || '';
        document.getElementById('gh-pat').value = data.github.token || '';
        toggleGhAuth();
        // Fetch branches and pre-select the saved one
        const savedBranch = data.github.branch || 'main';
        if (data.github.repo) {
            fetchGitBranches(savedBranch);
        } else {
            document.getElementById('gh-branch').innerHTML = '<option value="main">main</option>';
        }
    } else if (data.source_type === 'azure_devops' && data.azure_devops) {
        document.getElementById('ado-tenant-id').value = data.azure_devops.tenant_id || '';
        document.getElementById('ado-client-id').value = data.azure_devops.client_id || '';
        document.getElementById('ado-client-secret').value = data.azure_devops.client_secret || '';
        document.getElementById('ado-url').value = data.azure_devops.url || '';
        updateAdoConnectStatus(data.azure_devops.connected);
    } else if (data.source_type === 'jira' && data.jira) {
        document.getElementById('jira-url').value = data.jira.url || '';
        document.getElementById('jira-project').value = data.jira.project || '';
        document.getElementById('jira-token').value = data.jira.token || '';
    } else if (data.source_type === 'confluence' && data.confluence) {
        document.getElementById('confluence-url').value = data.confluence.url || '';
        document.getElementById('confluence-space').value = data.confluence.space || '';
        document.getElementById('confluence-token').value = data.confluence.token || '';
    } else if (data.source_type === 'box' && data.box) {
        document.getElementById('box-client-id').value = data.box.client_id || '';
        document.getElementById('box-client-secret').value = data.box.client_secret || '';
        document.getElementById('box-folder-id').value = data.box.folder_id || '';
        updateBoxConnectStatus(data.box.connected);
    }

    // Lock inputs and hide save/remove when folder has synced content
    // Keep the Connect/Reconnect button visible so users can re-consent to new scopes
    if (locked) {
        document.querySelectorAll('.sync-input, .sync-textarea, .sync-fields .sync-select').forEach(el => el.disabled = true);
        const saveBtn = document.getElementById('btn-sync-save');
        const removeBtn = document.getElementById('btn-sync-remove');
        if (saveBtn) saveBtn.style.display = 'none';
        if (removeBtn) removeBtn.style.display = 'none';
    }

    updateSyncStatusDisplay(data);
}

function clearSyncFields() {
    currentSyncSource = null;
    const select = document.getElementById('sync-source-type');
    if (select) {
        select.value = '';
        select.disabled = false;
    }
    document.querySelectorAll('.sync-fields').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.sync-input, .sync-textarea').forEach(el => {
        el.value = '';
        el.disabled = false;
    });
    const actions = document.getElementById('sync-actions');
    if (actions) actions.style.display = 'none';
    const saveBtn = document.getElementById('btn-sync-save');
    const removeBtn = document.getElementById('btn-sync-remove');
    if (saveBtn) saveBtn.style.display = '';
    if (removeBtn) removeBtn.style.display = '';
    const connectBtn = document.getElementById('btn-sp-connect');
    if (connectBtn) connectBtn.style.display = '';
    const statusDisplay = document.getElementById('sync-status-display');
    if (statusDisplay) statusDisplay.style.display = 'none';
}

function updateSyncStatusDisplay(data) {
    const display = document.getElementById('sync-status-display');
    const statusValue = document.getElementById('sync-status-value');
    const lastSynced = document.getElementById('sync-last-synced');

    if (!data || !data.source_type) {
        if (display) display.style.display = 'none';
        return;
    }

    if (display) display.style.display = 'flex';

    const statusLabels = {
        'idle': 'Idle',
        'syncing': 'Syncing...',
        'synced': 'Synced',
        'error': 'Error',
    };
    if (statusValue) {
        statusValue.textContent = statusLabels[data.sync_status] || data.sync_status;
        statusValue.className = `sync-status-value sync-status-${data.sync_status}`;
    }

    if (lastSynced) {
        if (data.last_synced_at) {
            const d = new Date(data.last_synced_at);
            lastSynced.textContent = `Last: ${d.toLocaleString()}`;
        } else {
            lastSynced.textContent = 'Never synced';
        }
    }

    const syncBtn = document.getElementById('btn-sync-trigger');
    if (syncBtn) {
        syncBtn.disabled = data.sync_status === 'syncing';
    }
}

function updateFolderSyncStatus(syncStatus, lastSyncedAt) {
    const row = document.getElementById('folder-sync-status-row');
    const value = document.getElementById('folder-sync-status-value');
    const lastSynced = document.getElementById('folder-sync-last-synced');
    if (!row) return;

    if (!syncStatus) {
        row.style.display = 'none';
        return;
    }

    row.style.display = 'flex';

    const labels = {
        'idle': 'Idle',
        'syncing': 'Syncing...',
        'synced': 'Synced',
        'error': 'Error',
    };
    if (value) {
        value.textContent = labels[syncStatus] || syncStatus;
        value.className = `sync-status-value sync-status-${syncStatus}`;
    }
    if (lastSynced) {
        if (lastSyncedAt) {
            const d = new Date(lastSyncedAt);
            lastSynced.textContent = `Last: ${d.toLocaleString()}`;
        } else {
            lastSynced.textContent = '';
        }
    }
}

function gatherSyncConfig() {
    const sourceType = document.getElementById('sync-source-type').value;
    if (!sourceType) return null;

    const config = { source_type: sourceType };

    if (sourceType === 'sharepoint') {
        config.sharepoint = {
            tenant_id: document.getElementById('sp-tenant-id').value.trim(),
            client_id: document.getElementById('sp-client-id').value.trim(),
            client_secret: document.getElementById('sp-client-secret').value.trim(),
            site_url: document.getElementById('sp-site-url').value.trim(),
            drive_id: document.getElementById('sp-drive-id').value.trim(),
        };
    } else if (sourceType === 'google_drive') {
        config.google_drive = {
            client_id: document.getElementById('gd-client-id').value.trim(),
            client_secret: document.getElementById('gd-client-secret').value.trim(),
            folder_id: document.getElementById('gd-folder-id').value.trim(),
        };
    } else if (sourceType === 'github') {
        config.github = {
            repo: document.getElementById('gh-repo').value.trim(),
            branch: document.getElementById('gh-branch').value.trim() || 'main',
            path: document.getElementById('gh-path').value.trim(),
            auth_method: document.getElementById('gh-auth-method').value,
            ssh_key: document.getElementById('gh-ssh-key').value.trim(),
            username: document.getElementById('gh-username').value.trim(),
            token: document.getElementById('gh-pat').value.trim(),
        };
    } else if (sourceType === 'azure_devops') {
        config.azure_devops = {
            tenant_id: document.getElementById('ado-tenant-id').value.trim(),
            client_id: document.getElementById('ado-client-id').value.trim(),
            client_secret: document.getElementById('ado-client-secret').value.trim(),
            url: document.getElementById('ado-url').value.trim(),
        };
    } else if (sourceType === 'jira') {
        config.jira = {
            url: document.getElementById('jira-url').value.trim(),
            project: document.getElementById('jira-project').value.trim(),
            token: document.getElementById('jira-token').value.trim(),
        };
    } else if (sourceType === 'confluence') {
        config.confluence = {
            url: document.getElementById('confluence-url').value.trim(),
            space: document.getElementById('confluence-space').value.trim(),
            token: document.getElementById('confluence-token').value.trim(),
        };
    } else if (sourceType === 'box') {
        config.box = {
            client_id: document.getElementById('box-client-id').value.trim(),
            client_secret: document.getElementById('box-client-secret').value.trim(),
            folder_id: document.getElementById('box-folder-id').value.trim(),
        };
    }

    return config;
}

async function saveSyncSource() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    const config = gatherSyncConfig();
    if (!config) {
        showToast('Select a sync source type', 'error');
        return;
    }

    try {
        const response = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save sync source');
        }

        const data = await response.json();
        currentSyncSource = data;
        updateSyncStatusDisplay(data);
        showToast('Sync source saved', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function removeSyncSource() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) return;

    try {
        const response = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'DELETE',
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to remove sync source');
        }

        clearSyncFields();
        updateFolderSyncStatus(null, null);
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function triggerRemoteSync() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    try {
        // Auto-save config before syncing
        await saveSyncSource();

        const response = await fetch(`/api/sync/${encodeURIComponent(targetPath)}/trigger`, {
            method: 'POST',
        });

        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.detail || 'Failed to trigger sync');
        }

        syncingFolders.add(targetPath);
        updateSyncStatusDisplay({ ...currentSyncSource, sync_status: 'syncing' });
        updateFolderSyncStatus('syncing', null);
        // No polling needed — WebSocket sync_status event will update the UI
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function updateSpConnectStatus(connected) {
    const el = document.getElementById('sp-connect-status');
    const btn = document.getElementById('btn-sp-connect');
    if (!el) return;

    if (connected) {
        el.className = 'sp-connect-status connected';
        el.textContent = 'Connected';
        if (btn) btn.textContent = 'Reconnect';
    } else {
        el.className = 'sp-connect-status not-connected';
        el.textContent = 'Not connected';
        if (btn) btn.textContent = 'Connect';
    }
}

let _lastBranchUrl = '';
let _lastBranchCred = '';

function toggleGhAuth() {
    const method = document.getElementById('gh-auth-method').value;
    document.getElementById('gh-ssh-fields').style.display = method === 'ssh' ? '' : 'none';
    document.getElementById('gh-token-fields').style.display = method === 'token' ? '' : 'none';
}

async function fetchGitBranches(preselectBranch) {
    const repoUrl = document.getElementById('gh-repo').value.trim();
    const authMethod = document.getElementById('gh-auth-method').value;
    const sshKey = document.getElementById('gh-ssh-key').value.trim();
    const ghUsername = document.getElementById('gh-username').value.trim();
    const ghPat = document.getElementById('gh-pat').value.trim();
    const branchSelect = document.getElementById('gh-branch');
    if (!repoUrl) return;

    // Build a cache key from all credential fields
    const credKey = authMethod + '|' + sshKey + '|' + ghUsername + '|' + ghPat;

    // Avoid duplicate fetches for the same URL+credential combo
    if (repoUrl === _lastBranchUrl && credKey === _lastBranchCred) {
        if (preselectBranch) branchSelect.value = preselectBranch;
        return;
    }

    // Show loading state
    const savedValue = preselectBranch || branchSelect.value;
    branchSelect.innerHTML = '<option value="" disabled selected>Loading branches...</option>';
    branchSelect.disabled = true;

    try {
        const params = new URLSearchParams({ repo_url: repoUrl });
        if (authMethod === 'token') {
            if (ghPat) params.set('token', ghPat);
            if (ghUsername) params.set('username', ghUsername);
        } else {
            if (sshKey) params.set('ssh_key', sshKey);
        }
        const resp = await fetch(`/api/sync/git/branches?${params}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
        const data = await resp.json();
        const branches = data.branches || [];

        branchSelect.innerHTML = '';
        if (branches.length === 0) {
            branchSelect.innerHTML = '<option value="main">main</option>';
        } else {
            branches.forEach(b => {
                const opt = document.createElement('option');
                opt.value = b;
                opt.textContent = b;
                branchSelect.appendChild(opt);
            });
        }

        // Select the previously saved branch, or default to first (main/master)
        if (savedValue && branches.includes(savedValue)) {
            branchSelect.value = savedValue;
        }

        _lastBranchUrl = repoUrl;
        _lastBranchCred = credKey;
    } catch (e) {
        branchSelect.innerHTML = '<option value="main">main</option>';
        console.warn('Failed to fetch branches:', e.message);
    } finally {
        branchSelect.disabled = false;
    }
}

async function fetchGoogleDriveFolders(preselectFolder) {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) return;

    const folderSelect = document.getElementById('gd-folder-id');
    if (!folderSelect) return;

    // Show loading state
    const savedValue = preselectFolder || folderSelect.value;
    folderSelect.innerHTML = '<option value="" disabled selected>Loading folders...</option>';
    folderSelect.disabled = true;

    try {
        const resp = await fetch(`/api/sync/google-drive/folders?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
        const data = await resp.json();
        const myFolders = data.folders || [];
        const sharedFolders = data.shared_folders || [];
        const sharedDrives = data.shared_drives || [];

        folderSelect.innerHTML = '<option value="">Select a folder...</option>';

        if (myFolders.length) {
            const myGroup = document.createElement('optgroup');
            myGroup.label = 'My Drive';
            myFolders.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.id;
                opt.textContent = f.name;
                myGroup.appendChild(opt);
            });
            folderSelect.appendChild(myGroup);
        }

        if (sharedDrives.length) {
            const drivesGroup = document.createElement('optgroup');
            drivesGroup.label = 'Shared Drives';
            sharedDrives.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.id;
                opt.textContent = f.name;
                drivesGroup.appendChild(opt);
            });
            folderSelect.appendChild(drivesGroup);
        }

        if (sharedFolders.length) {
            const sharedGroup = document.createElement('optgroup');
            sharedGroup.label = 'Shared with me';
            sharedFolders.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.id;
                opt.textContent = f.name;
                sharedGroup.appendChild(opt);
            });
            folderSelect.appendChild(sharedGroup);
        }

        if (savedValue) {
            folderSelect.value = savedValue;
        }
    } catch (e) {
        folderSelect.innerHTML = '<option value="">Select a folder...</option>';
        console.warn('Failed to fetch Google Drive folders:', e.message);
    } finally {
        folderSelect.disabled = false;
    }
}

async function connectSharePoint() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    // First save the current config
    const config = gatherSyncConfig();
    if (!config) {
        showToast('Select SharePoint and fill in the fields first', 'error');
        return;
    }

    try {
        // Save config first
        const saveResp = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        if (!saveResp.ok) {
            const error = await saveResp.json();
            throw new Error(error.detail || 'Failed to save config');
        }

        // Get the auth URL (unified OAuth endpoint)
        const resp = await fetch(`/api/sync/oauth/auth?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const error = await resp.json();
            throw new Error(error.detail || 'Failed to start SharePoint auth');
        }

        const data = await resp.json();
        // Open Microsoft login in a new tab
        window.open(data.auth_url, '_blank');

        showToast('Sign in to Microsoft in the new tab. This page will update when done.', 'info');
        // No polling needed — WebSocket sp_connected event will update the UI
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function updateAdoConnectStatus(connected) {
    const el = document.getElementById('ado-connect-status');
    const btn = document.getElementById('btn-ado-connect');
    if (!el) return;

    if (connected) {
        el.className = 'sp-connect-status connected';
        el.textContent = 'Connected';
        if (btn) btn.textContent = 'Reconnect';
    } else {
        el.className = 'sp-connect-status not-connected';
        el.textContent = 'Not connected';
        if (btn) btn.textContent = 'Connect';
    }
}

async function connectAzureDevOps() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    const config = gatherSyncConfig();
    if (!config) {
        showToast('Select Azure DevOps and fill in the fields first', 'error');
        return;
    }

    try {
        // Save config first
        const saveResp = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        if (!saveResp.ok) {
            const error = await saveResp.json();
            throw new Error(error.detail || 'Failed to save config');
        }

        // Get the auth URL (unified OAuth endpoint)
        const resp = await fetch(`/api/sync/oauth/auth?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const error = await resp.json();
            throw new Error(error.detail || 'Failed to start Azure DevOps auth');
        }

        const data = await resp.json();
        window.open(data.auth_url, '_blank');

        showToast('Sign in to Microsoft in the new tab. This page will update when done.', 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function updateBoxConnectStatus(connected) {
    const el = document.getElementById('box-connect-status');
    const btn = document.getElementById('btn-box-connect');
    if (!el) return;

    if (connected) {
        el.className = 'sp-connect-status connected';
        el.textContent = 'Connected';
        if (btn) btn.textContent = 'Reconnect';
    } else {
        el.className = 'sp-connect-status not-connected';
        el.textContent = 'Not connected';
        if (btn) btn.textContent = 'Connect';
    }
}

async function connectBox() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    const config = gatherSyncConfig();
    if (!config) {
        showToast('Select Box and fill in the fields first', 'error');
        return;
    }

    try {
        // Save config first
        const saveResp = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        if (!saveResp.ok) {
            const error = await saveResp.json();
            throw new Error(error.detail || 'Failed to save config');
        }

        // Get the auth URL (unified OAuth endpoint)
        const resp = await fetch(`/api/sync/oauth/auth?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const error = await resp.json();
            throw new Error(error.detail || 'Failed to start Box auth');
        }

        const data = await resp.json();
        window.open(data.auth_url, '_blank');

        showToast('Sign in to Box in the new tab. This page will update when done.', 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function updateGdConnectStatus(connected) {
    const el = document.getElementById('gd-connect-status');
    const btn = document.getElementById('btn-gd-connect');
    if (!el) return;

    if (connected) {
        el.className = 'sp-connect-status connected';
        el.textContent = 'Connected';
        if (btn) btn.textContent = 'Reconnect';
    } else {
        el.className = 'sp-connect-status not-connected';
        el.textContent = 'Not connected';
        if (btn) btn.textContent = 'Connect';
    }
}

async function connectGoogleDrive() {
    const targetPath = selectedPath || currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    const config = gatherSyncConfig();
    if (!config) {
        showToast('Select Google Drive and fill in the fields first', 'error');
        return;
    }

    try {
        // Save config first
        const saveResp = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        if (!saveResp.ok) {
            const error = await saveResp.json();
            throw new Error(error.detail || 'Failed to save config');
        }

        // Get the auth URL (unified OAuth endpoint)
        const resp = await fetch(`/api/sync/oauth/auth?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const error = await resp.json();
            throw new Error(error.detail || 'Failed to start Google Drive auth');
        }

        const data = await resp.json();
        window.open(data.auth_url, '_blank');

        showToast('Sign in to Google in the new tab. This page will update when done.', 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function loadSyncSource(folderPath) {
    try {
        const response = await fetch(`/api/sync/${encodeURIComponent(folderPath)}`);
        if (response.ok) {
            const data = await response.json();
            if (data && data.source_type) {
                populateSyncFields(data);
                return;
            }
        }
    } catch (error) {
        // No sync source configured
    }
    clearSyncFields();
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
