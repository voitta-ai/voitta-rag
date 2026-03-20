/**
 * Sidebar: item selection, details loading, metadata, folder settings.
 */
import { state } from './state.js';
import { showToast, showSpinner, hideSpinner, escapeHtml } from './ui.js';
import { refreshFileList, updateFileListIndexStatus } from './file-ops.js';
import { loadSyncSource, clearSyncFields, updateFolderSyncStatus } from './sync.js';

function _isAnamnesisPath(p) {
    return p === 'Anamnesis' || (p && p.startsWith('Anamnesis/'));
}

export function selectItem(element, path, isDir) {
    const alreadySelected = state.selectedPath === path;

    if (state.selectedItem) {
        state.selectedItem.classList.remove('selected');
    }

    element.classList.add('selected');
    state.selectedItem = element;
    state.selectedPath = path;
    state.selectedIsDir = isDir;

    if (!alreadySelected) {
        loadItemDetails(path, isDir);
    }

    if (isDir) {
        if (element.dataset.lastClick && Date.now() - element.dataset.lastClick < 300) {
            window.location.href = `/browse/${path}`;
        }
        element.dataset.lastClick = Date.now();
    }
}

export async function loadItemDetails(path, isDir = true) {
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

export function updateSidebar(details) {
    const isAnamnesis = _isAnamnesisPath(details.path);

    const titleEl = document.getElementById('selected-item-title');
    const pathEl = document.getElementById('selected-item-path');
    if (titleEl) titleEl.textContent = details.name;
    if (pathEl) pathEl.textContent = details.path;

    // Folder settings section
    const folderSettingsSection = document.getElementById('folder-settings-section');
    const fileIndexSection = document.getElementById('file-index-section');

    if (folderSettingsSection) {
        if (details.is_dir) {
            folderSettingsSection.style.display = 'block';

            const folderEnabledCheckbox = document.getElementById('folder-enabled');
            if (folderEnabledCheckbox) {
                folderEnabledCheckbox.checked = details.folder_enabled || false;
            }

            const statusValue = document.getElementById('index-status-value');
            if (statusValue) {
                statusValue.className = `index-status-value status-${details.index_status || 'none'}`;
                const statusLabels = {
                    'indexed': 'Indexed', 'indexing': 'Indexing...', 'pending': 'Pending',
                    'error': 'Error', 'none': 'Not indexed'
                };
                statusValue.textContent = statusLabels[details.index_status] || 'Not indexed';
            }

            const dangerZone = folderSettingsSection.querySelector('.folder-danger-zone');
            if (dangerZone) dangerZone.style.display = isAnamnesis ? 'none' : '';
        } else {
            folderSettingsSection.style.display = 'none';
        }
    }

    // File index section
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
                fileChunkCount.textContent = details.chunk_count ? `${details.chunk_count} chunks` : '\u2014';
            }
            if (fileIndexedAt) {
                if (details.indexed_at) {
                    const date = new Date(details.indexed_at);
                    fileIndexedAt.textContent = date.toLocaleString();
                } else {
                    fileIndexedAt.textContent = '\u2014';
                }
            }
        } else {
            fileIndexSection.style.display = 'none';
        }
    }

    // Metadata section
    const metadataContent = document.getElementById('metadata-content');
    const metadataPlaceholder = document.getElementById('metadata-placeholder');
    const metadataText = document.getElementById('metadata-text');
    const metadataInfo = document.getElementById('metadata-info');

    if (metadataContent) metadataContent.style.display = 'block';
    if (metadataPlaceholder) metadataPlaceholder.style.display = 'none';

    if (metadataText) {
        metadataText.value = details.metadata_text || '';
        metadataText.readOnly = isAnamnesis;
    }

    if (metadataInfo) {
        metadataInfo.textContent = details.metadata_updated_by
            ? `Last updated by ${details.metadata_updated_by}`
            : '';
    }

    // Sync status
    updateFolderSyncStatus(details.sync_status, details.last_synced_at);

    // Sync source section
    state.currentFolderIsEmpty = details.is_empty;
    const syncSection = document.getElementById('sync-source-section');
    if (syncSection) {
        if (!isAnamnesis && details.is_dir && (details.sync_source_type || details.is_empty)) {
            syncSection.style.display = 'block';
            loadSyncSource(details.path);
        } else {
            syncSection.style.display = 'none';
            clearSyncFields();
        }
    }

    // Indexing stats
    const indexingStatsSection = document.getElementById('indexing-stats-section');
    const indexingStatsBody = document.getElementById('indexing-stats-body');

    if (indexingStatsSection && indexingStatsBody) {
        if (details.is_dir && details.file_type_stats && details.file_type_stats.length > 0) {
            indexingStatsSection.style.display = 'block';

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

// ---- Metadata ----

export async function saveMetadata(text) {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) {
        showToast('No item selected', 'error');
        return;
    }

    if (state.metadataSaveTimeout) {
        clearTimeout(state.metadataSaveTimeout);
    }

    state.metadataSaveTimeout = setTimeout(async () => {
        try {
            const response = await fetch(`/api/metadata/${encodeURIComponent(targetPath)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
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

// ---- Folder settings ----

export async function toggleFolderEnabled(enabled) {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        document.getElementById('folder-enabled').checked = !enabled;
        return;
    }

    try {
        const response = await fetch(`/api/settings/folders/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update setting');
        }

        showToast(enabled ? 'Folder enabled for indexing' : 'Folder disabled for indexing', 'success');
        await loadItemDetails(targetPath);
        updateFileListIndexStatus(targetPath, enabled ? 'pending' : 'none');
    } catch (error) {
        showToast(error.message, 'error');
        document.getElementById('folder-enabled').checked = !enabled;
    }
}

export async function toggleSearchActive(active) {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        document.getElementById('folder-search-active').checked = !active;
        return;
    }

    try {
        const response = await fetch(`/api/settings/folders/${encodeURIComponent(targetPath)}/search-active`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ search_active: active }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update setting');
        }

        showToast(active ? 'Folder activated for search' : 'Folder deactivated for search', 'success');
    } catch (error) {
        showToast(error.message, 'error');
        document.getElementById('folder-search-active').checked = !active;
    }
}

export async function toggleSearchActiveInline(checkbox, folderPath) {
    const active = checkbox.checked;
    const fileItem = checkbox.closest('.file-item');

    try {
        const response = await fetch(`/api/settings/folders/${encodeURIComponent(folderPath)}/search-active`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ search_active: active }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update setting');
        }

        if (fileItem) {
            fileItem.dataset.searchActive = active ? 'true' : 'false';
        }

        showToast(active ? 'Folder activated for search' : 'Folder deactivated for search', 'success');
    } catch (error) {
        showToast(error.message, 'error');
        checkbox.checked = !active;
    }
}

export async function reindexFolder() {
    const targetPath = state.selectedPath || state.currentPath;
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
        await loadItemDetails(targetPath);
        updateFileListIndexStatus(targetPath, result.status);
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export async function triggerIndex() {
    try {
        const response = await fetch(`/api/index/${state.currentPath}`, {
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
