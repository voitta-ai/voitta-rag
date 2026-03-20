/**
 * Central state store for voitta-rag UI.
 * All modules import and mutate this shared object.
 */
export const state = {
    // Selection
    selectedItem: null,
    selectedPath: null,
    selectedIsDir: false,

    // Navigation (set from template)
    currentPath: '',
    currentProjectId: null,
    isAnamnesis: false,

    // WebSocket
    ws: null,
    wsReconnectTimeout: null,
    wsReconnectDelay: 1000,

    // Sync
    currentSyncSource: null,
    syncingFolders: new Set(),
    deletingFolders: new Set(),
    currentFolderIsEmpty: true,

    // Sort
    currentSortColumn: 'name',
    currentSortAsc: true,

    // Debounce timers
    refreshDebounceTimeout: null,
    metadataSaveTimeout: null,
    spinnerTimeout: null,

    // Delete folder
    deleteFolderTargetPath: null,

    // Git branch cache
    lastBranchUrl: '',
    lastBranchCred: '',
};
