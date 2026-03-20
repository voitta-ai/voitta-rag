/**
 * WebSocket event handlers.
 */
import { state } from './state.js';
import { showToast } from './ui.js';
import { refreshFileList, updateFileListIndexStatus } from './file-ops.js';
import { loadItemDetails } from './sidebar.js';
import {
    updateSyncStatusDisplay, updateFolderSyncStatus,
    updateSpConnectStatus, updateAdoConnectStatus, updateBoxConnectStatus, updateGdConnectStatus,
    loadSyncSource, fetchSharePointSites, fetchGoogleDriveFolders,
} from './sync.js';

export function handleFileSystemEvent(event) {
    const inBusyFolder = [...state.syncingFolders, ...state.deletingFolders].some(
        folder => event.path === folder || event.path.startsWith(folder + '/')
    );

    if (!inBusyFolder && state.currentPath !== undefined) {
        const eventDir = event.path.split('/').slice(0, -1).join('/');
        if (eventDir === state.currentPath || event.path.startsWith(state.currentPath + '/')) {
            refreshFileList();
        }
    }
}

export function handleSyncStatusEvent(event) {
    const folderPath = event.path;

    if (event.sync_status === 'syncing') {
        state.syncingFolders.add(folderPath);
    }

    if (state.selectedPath === folderPath || state.currentPath === folderPath) {
        updateSyncStatusDisplay({
            ...state.currentSyncSource,
            sync_status: event.sync_status,
            sync_error: event.sync_error,
            last_synced_at: event.last_synced_at,
        });
        updateFolderSyncStatus(event.sync_status, event.last_synced_at);
    }

    if (event.sync_status !== 'syncing') {
        state.syncingFolders.delete(folderPath);
        if (state.currentPath !== undefined) {
            if (folderPath === state.currentPath || folderPath.startsWith(state.currentPath + '/') ||
                state.currentPath.startsWith(folderPath + '/')) {
                refreshFileList();
            }
        }
        if (event.sync_status === 'error' && event.sync_error) {
            showToast('Sync failed: ' + event.sync_error, 'error');
        } else if (event.sync_status === 'synced') {
            showToast('Sync completed for ' + folderPath, 'success');
        }
    }
}

export function handleIndexStatusEvent(event) {
    const folderPath = event.path;
    const status = event.status;

    updateFileListIndexStatus(folderPath, status);

    if (state.selectedPath === folderPath || state.currentPath === folderPath) {
        const statusValue = document.getElementById('index-status-value');
        if (statusValue) {
            statusValue.className = `index-status-value status-${status}`;
            const statusLabels = {
                'indexed': 'Indexed', 'indexing': 'Indexing...', 'pending': 'Pending',
                'error': 'Error', 'none': 'Not indexed'
            };
            statusValue.textContent = statusLabels[status] || 'Not indexed';
        }
    }
}

export function handleIndexCompleteEvent(event) {
    const folderPath = event.path;

    updateFileListIndexStatus(folderPath, 'indexed');

    if (state.selectedPath === folderPath || state.currentPath === folderPath) {
        const statusValue = document.getElementById('index-status-value');
        if (statusValue) {
            statusValue.className = 'index-status-value status-indexed';
            statusValue.textContent = 'Indexed';
        }
        loadItemDetails(folderPath, true);
    }

    if (state.currentPath !== undefined) {
        if (folderPath === state.currentPath || folderPath.startsWith(state.currentPath + '/') ||
            state.currentPath.startsWith(folderPath + '/')) {
            refreshFileList();
        }
    }
}

export function handleSpConnectedEvent(event) {
    const folderPath = event.path;
    if (state.selectedPath === folderPath || state.currentPath === folderPath) {
        updateSpConnectStatus(true);
        const sitesSection = document.getElementById('sp-sites-section');
        if (sitesSection) sitesSection.style.display = 'block';
        fetchSharePointSites();
        loadSyncSource(folderPath);
        showToast('SharePoint connected successfully', 'success');
    }
}

export function handleAdoConnectedEvent(event) {
    const folderPath = event.path;
    if (state.selectedPath === folderPath || state.currentPath === folderPath) {
        updateAdoConnectStatus(true);
        loadSyncSource(folderPath);
        showToast('Azure DevOps connected successfully', 'success');
    }
}

export function handleBoxConnectedEvent(event) {
    const folderPath = event.path;
    if (state.selectedPath === folderPath || state.currentPath === folderPath) {
        updateBoxConnectStatus(true);
        loadSyncSource(folderPath);
        showToast('Box connected successfully', 'success');
    }
}

export function handleGdConnectedEvent(event) {
    const folderPath = event.path;
    if (state.selectedPath === folderPath || state.currentPath === folderPath) {
        updateGdConnectStatus(true);
        loadSyncSource(folderPath);
        fetchGoogleDriveFolders();
        showToast('Google Drive connected successfully', 'success');
    }
}
