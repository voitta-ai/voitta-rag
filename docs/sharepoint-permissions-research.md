# SharePoint Online Permissions & Graph API: Technical Research

Research date: 2026-02-28

This document covers the SharePoint Online permission model as it relates to building
an external ACL sync system (specifically for Qdrant vector search / RAG), focusing on
what the Graph API actually exposes and where it falls short.

---

## 1. SharePoint Permission Inheritance Model

### The Hierarchy

Permissions in SharePoint Online flow top-down through this chain:

```
Site Collection (root site)
  -> Site
    -> Document Library (list)
      -> Folder
        -> Sub-folder
          -> File (item)
```

By default, every object **inherits** permissions from its parent. A document library
inherits from the site, folders inherit from the library, files inherit from their folder.

### Breaking Inheritance

Any object in the chain can **break inheritance**, which means:

1. The object gets a **copy** of the parent's current permissions at that moment.
2. Those permissions become independent -- future changes to the parent do NOT propagate down.
3. The object now has "unique permissions" (as opposed to inherited).

Inheritance is broken automatically when:
- A user shares a file/folder with someone who doesn't already have access via the parent.
- An admin explicitly breaks inheritance on a library, folder, or file.

Constraints:
- You cannot break inheritance on a library or folder containing more than **100,000 items**.
- You can still break inheritance on individual files inside a large library.

### What This Means for ACL Sync

When a file inherits permissions, its effective ACL is identical to its parent folder
(or library, or site -- wherever the nearest unique permission scope is). When building
an external ACL, you need to walk up the hierarchy to find the "effective" permissions.

The Graph API helps here but with important caveats (see Section 4).

---

## 2. Permission Levels (Roles)

SharePoint has built-in permission levels that bundle specific actions:

| Level | Key Capabilities |
|-------|-----------------|
| **Full Control** | Everything. Create/delete lists, manage permissions, etc. |
| **Edit** | Add, edit, delete items. Manage lists. Cannot manage permissions. |
| **Contribute** | Add, edit, delete items. Cannot create lists/libraries or manage permissions. |
| **Read** | View items, pages, documents. Cannot modify. |
| **View Only** | View items but cannot download documents (view in browser only). |

The Graph API simplifies these into **roles**: `read`, `write`, and `owner`.
The mapping is lossy -- the Graph API does not expose the full SharePoint permission
level granularity. For ACL sync purposes this is usually fine since we typically only
care about "can this user access this content" (read or above).

---

## 3. SharePoint Site Groups vs. M365 Groups

This is one of the most confusing aspects of SharePoint permissions. There are two
different group systems at play.

### SharePoint Site Groups

Every SharePoint site has three default SharePoint groups:

- **{Site Name} Owners** -- Full Control
- **{Site Name} Members** -- Edit
- **{Site Name} Visitors** -- Read

These are SharePoint-only constructs. They exist only within SharePoint, have no
representation in Entra ID (Azure AD), and are **not directly accessible via the
Graph API**.

To enumerate SharePoint group members, you must use the **SharePoint REST API**:
```
GET /_api/web/sitegroups/getbyname('{Group Name}')/users
```

The Graph API has no equivalent endpoint for SharePoint site groups.

### Microsoft 365 Groups (M365 Groups)

M365 Groups are Entra ID security groups that span multiple services (Teams, Exchange,
SharePoint, Planner, etc.). They have only two roles:

- **Owners** -- manage the group
- **Members** -- participate in group resources

When an M365 Group is created (e.g., via Teams or Outlook), a SharePoint site is
automatically provisioned. The M365 Group is then linked to the SharePoint site groups:

```
M365 Group Owners   -> added to "{Site} Owners" SharePoint group
M365 Group Members  -> added to "{Site} Members" SharePoint group
```

### Key Differences

| Aspect | SharePoint Site Groups | M365 Groups |
|--------|----------------------|-------------|
| Scope | SharePoint only | Cross-service (Teams, Exchange, etc.) |
| Roles | Owners, Members, Visitors (3) | Owners, Members (2) |
| Entra ID representation | None | Yes (security group) |
| Graph API support | Not directly | Full support |
| Can contain nested groups | Limited | Yes |

### Can Someone Be In One But Not the Other?

**Yes.** This is common:

- A SharePoint admin can add a user directly to the "Visitors" SharePoint group
  without adding them to the M365 Group. That user has Read access to the site
  but is not an M365 Group member.
- Conversely, removing someone from a SharePoint group does not remove them from
  the M365 Group (and vice versa, although M365 Group changes typically propagate
  to SharePoint group membership).
- A user can be granted direct permissions to a specific library, folder, or file
  without being in any group at all.

### Implication for ACL Sync

You cannot rely solely on M365 Group membership to determine SharePoint access.
Users may have access via:

1. M365 Group membership (propagated to SharePoint groups)
2. Direct membership in SharePoint site groups
3. Sharing links (organization-wide, specific-people, or anonymous)
4. Direct item-level permissions (invitation/sharing)

A complete ACL system must account for all four.

---

## 4. Graph API Behavior for Permissions

### Endpoint: List Permissions on a DriveItem

```
GET /drives/{drive-id}/items/{item-id}/permissions
GET /sites/{siteId}/drive/items/{itemId}/permissions
```

**Required scopes:** `Files.Read` (minimum), `Files.Read.All`, `Sites.Read.All`

#### What It Returns

This endpoint returns the **effective sharing permissions** for the item. It includes
**both inherited and directly-applied permissions**. Each permission object in the
response represents one access grant.

#### The `inheritedFrom` Problem

The `Permission` resource has an `inheritedFrom` property (type `ItemReference`) that
should tell you which ancestor the permission was inherited from. However:

> **OneDrive for Business and SharePoint document libraries do NOT return the
> `inheritedFrom` property.**

This is stated directly in the official Microsoft documentation. It means:

- You get the flat list of all permissions (inherited + explicit).
- You **cannot distinguish** which permissions are inherited vs. uniquely set.
- You cannot tell at what level in the hierarchy a permission was originally granted.

This is a significant limitation for ACL sync. You get the right answer (who has access)
but you lose the structural information (why they have access and from where).

#### Permission Object Structure

```json
{
  "id": "permission-id",
  "roles": ["read"],          // "read", "write", or "owner"

  // For direct user/group grants:
  "grantedToV2": {
    "user": {
      "id": "entra-user-id",
      "displayName": "Jane Doe"
    },
    "siteUser": {
      "id": "sharepoint-site-user-id",
      "displayName": "Jane Doe",
      "loginName": "i:0#.f|membership|jane@contoso.com"
    }
  },

  // For sharing links with specific people:
  "grantedToIdentitiesV2": [
    {
      "user": { "id": "...", "displayName": "..." },
      "siteUser": { "id": "...", "displayName": "...", "loginName": "..." }
    }
  ],

  // For sharing links:
  "link": {
    "type": "view",             // "view", "edit", "embed"
    "scope": "organization",    // "anonymous", "organization", "users", "existingAccess"
    "webUrl": "https://..."
  },

  // For email invitations:
  "invitation": {
    "email": "jd@contoso.com",
    "signInRequired": true
  },

  // NOT returned for SharePoint/OneDrive for Business:
  "inheritedFrom": null,

  "expirationDateTime": "2025-12-31T00:00:00Z",
  "hasPassword": false
}
```

**Important:** `grantedTo` and `grantedToIdentities` are **deprecated**. Use
`grantedToV2` and `grantedToIdentitiesV2` instead.

#### Permission Types You'll See

| Type | How to Identify | Who Has Access |
|------|----------------|---------------|
| Direct user/group grant | `grantedToV2` is set, no `link` | The specific user/group |
| Sharing link (anyone) | `link.scope == "anonymous"` | Anyone with the URL |
| Sharing link (organization) | `link.scope == "organization"` | Any authenticated user in the tenant |
| Sharing link (specific people) | `link.scope == "users"` | Users in `grantedToIdentitiesV2` |
| Sharing link (existing access) | `link.scope == "existingAccess"` | No new access granted, just a link for people who already have access |
| Email invitation | `invitation` is set | The invited user (once redeemed) |

#### Caller-Dependent Response

The response varies based on who calls the API:

- **Item owner / co-owner:** Gets ALL sharing permissions.
- **Non-owner:** Gets only the permissions that apply to the caller.
- **Application permissions (app-only):** Gets all permissions (acts as owner).

For ACL sync, you must use **application permissions** to see the complete picture.

### No "Check Effective Access for User X" Endpoint

There is **no Graph API endpoint** that answers: "Does user X have access to item Y?"

You must:
1. Get the permissions list for the item.
2. Resolve group memberships yourself.
3. Check if the user is covered by any of the permissions.

### No Bulk Permissions Endpoint

There is **no endpoint** to get permissions for multiple items in a single call.
You must call `GET /drives/{id}/items/{itemId}/permissions` for each item individually.

You can use **$batch** to bundle up to 20 of these calls (see Section 5), but there
is no "give me permissions for all items in this library" endpoint. Note that each
permission API call costs **5 resource units** against your throttling budget (the
highest cost tier for Graph API calls), so a batch of 20 permission requests costs
100 resource units.

### Getting Site-Level Permissions

For site-level permissions (application permissions granted to the site):

```
GET /sites/{siteId}/permissions
```

This returns the list of applications that have been granted access to the site via
`Sites.Selected`. This is NOT the same as user permissions -- it's about which
registered apps have API access.

### Enumerating M365 Group Members

To get the members of the M365 Group linked to a site:

```
GET /groups/{group-id}/members            # Direct members only
GET /groups/{group-id}/transitiveMembers  # Flattened, includes nested groups
```

The `transitiveMembers` endpoint is critical -- it resolves nested group membership
into a flat list of users. Use it.

To get only users (excluding nested group objects):
```
GET /groups/{group-id}/transitiveMembers/microsoft.graph.user
```

To find which M365 Group is associated with a SharePoint site, get the site and check
the `group` relationship or use:
```
GET /groups?$filter=mailNickname eq '{site-mailnickname}'
```

### Checking if an Item Has Unique Permissions

The Graph API does **not** expose whether an item has unique (broken) permissions vs.
inherited permissions. The SharePoint REST API does:

```
GET /_api/web/lists('{listId}')/items({itemId})/HasUniqueRoleAssignments
```

This returns a boolean. The Graph API has no equivalent.

---

## 5. The `$batch` Endpoint

### How It Works

```
POST https://graph.microsoft.com/v1.0/$batch
Content-Type: application/json

{
  "requests": [
    {
      "id": "1",
      "method": "GET",
      "url": "/drives/{driveId}/items/{item1}/permissions"
    },
    {
      "id": "2",
      "method": "GET",
      "url": "/drives/{driveId}/items/{item2}/permissions"
    }
  ]
}
```

### Key Details

- **Maximum 20 requests per batch.** Hard limit.
- Each request in the batch is a standard Graph API call with `id`, `method`, `url`,
  optional `headers`, and optional `body`.
- URLs are **relative** (no `https://graph.microsoft.com/v1.0` prefix).
- The overall batch response is HTTP 200 even if individual requests fail. Each
  individual response has its own `status` code.
- Requests are **evaluated individually** against throttling limits. A batch does
  NOT bypass throttling -- it just reduces HTTP round-trips.

### Ordering with `dependsOn`

You can use the `dependsOn` property (array of request IDs) to enforce execution
order. Without it, requests may execute in any order or in parallel.

```json
{
  "id": "2",
  "dependsOn": ["1"],
  "method": "GET",
  "url": "/..."
}
```

If a depended-on request fails, dependent requests fail with `424 Failed Dependency`.

### Practical Limits Beyond the 20-Request Cap

- **Throttling:** SharePoint has per-app-per-tenant throttling. Each request in the
  batch counts individually. Getting permissions for 20 items in one batch = 20
  requests against your throttling budget.
- **Global limit:** 130,000 requests per 10 seconds per app across all tenants.
- **SharePoint-specific:** Uses a resource unit cost model. Each API call has a
  predetermined cost. **Permission resource operations cost 5 resource units each**
  (including `$expand=permissions`). By comparison, single-item queries cost 1 unit
  and multi-item queries cost 2 units. Getting permissions for 20 items in one batch
  costs 100 resource units (20 x 5).
- **Per-app per-tenant limits (resource units):** Vary by tenant license count,
  ranging from 1,250/min (0-1K licenses) to 6,250/min (50K+ licenses), and from
  1,200,000/24h (0-1K) to 6,000,000/24h (50K+).
- **Per-tenant limits (resource units):** Range from 18,750/5min (0-1K licenses)
  to 93,750/5min (50K+ licenses).
- When throttled, you get `429 Too Many Requests` with a `Retry-After` header.
  You MUST respect this or risk being blocked.

### Batch Strategy for Permission Sync

For a library with N files:
1. Use delta query to get the file list (no permissions in delta response).
2. Batch permission requests in groups of 20.
3. Space batches to stay within throttling limits.
4. Use exponential backoff on 429 responses.

---

## 6. Teams-Connected SharePoint Sites

### Standard Channels

When a Teams team is created, it provisions:
- An M365 Group (in Entra ID)
- A SharePoint site (linked to the M365 Group)
- An Exchange mailbox
- A Planner plan

Standard channel files are stored in the team's SharePoint site, in a folder per
channel under the "Shared Documents" library.

**Permissions:** All Teams members (= M365 Group members) have Edit access to the
SharePoint site. There is no per-standard-channel permission isolation -- any team
member can access any standard channel's files.

### Private Channels

Private channels create a **separate SharePoint site collection** with:
- Template ID: `TEAMCHANNEL#0` or `TEAMCHANNEL#1`
- Independent permissions (only private channel members have access)
- Membership synced from Teams (the channel manages it, not SharePoint directly)

Key facts:
- **Separate site = separate drive.** Files are NOT in the parent team's document
  library.
- **Permission management is done through Teams**, not SharePoint. You cannot
  independently manage the private channel site's permissions through SharePoint.
- Site owner and member groups are **kept in sync** with the private channel
  membership in Teams.
- The parent team's owners do NOT automatically get access to private channel files
  (unless they are also channel members).
- Data classification and guest access permissions are inherited from the parent
  team's site.

API implications:
- To find private channel sites, you need to enumerate them. They are separate sites
  that link back to the parent team.
- When crawling for ACL sync, you must discover these additional sites -- they won't
  appear as folders under the main team site's drive.

### Shared Channels

Shared channels are similar to private channels but support **cross-tenant** access
via B2B direct connect. They also create separate site collections. The membership
model is more complex because it can include users from external tenants.

### 2025-2026 Changes

In late 2025, Microsoft moved private channels to "group compliance" (MC1134737).
Key changes:

- Newly created private channels may **not** create a document library by default;
  the root folder is used instead. This affects how you discover their drives.
- Compliance copies of messages are now delivered to the group mailbox instead of
  the mailboxes of all private channel members.
- **Member limit increased from 250 to 5,000** per private channel (rolling out
  March 2026).
- **Private channel limit increased to 1,000** per team (included in the 1,000
  total channel limit per team).
- Meetings can now be scheduled in private channels.

---

## 7. Delta Query for Permission Change Detection

The Graph API delta query for drives (`/drives/{id}/root/delta`) can detect when
permissions change on items, but requires specific headers.

### Required Headers

```
Prefer: deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges
```

All three must be provided together. You can combine them in a single `Prefer` header.

### Behavior

When `deltashowsharingchanges` is included:
- Items that appear in the delta response **due to permission changes** are annotated
  with `"@microsoft.graph.sharedChanged": "True"`.
- This lets you distinguish content changes from permission changes.

When `hierarchicalsharing` is also included:
- Sharing information is returned for the **root of the permissions hierarchy** and
  items with **explicit** sharing changes.
- Items inheriting permissions from a parent will NOT appear unless they have their
  own sharing changes.
- This significantly reduces the number of items you need to re-check.

### Required App Permission

To use delta with sharing changes: **`Sites.FullControl.All`**

This is a highly privileged scope. There is no way around it for permission scanning.

### Limitations

- Delta does not reliably track permission changes at the **library** level (e.g.,
  when a group is added/removed from the library's permissions).
- Delta does not track SharePoint group membership changes (e.g., a user added to the
  "Members" SharePoint group). Those are not item-level permission changes.
- If the M365 Group membership changes, the items themselves don't change, so delta
  won't fire.

### Recommended Pattern

```
1. Initial full crawl:
   - Enumerate all drives in all sites
   - For each drive, call delta with no token (gets all items)
   - For each item, get permissions
   - Store everything in your ACL store (Qdrant metadata)

2. Subscribe to webhooks:
   - Subscribe to drive changes
   - Subscribe to security webhooks (Prefer: includesecuritywebhooks)

3. On webhook notification:
   - Call delta with your stored deltaLink
   - Items with @microsoft.graph.sharedChanged == True -> re-fetch permissions
   - Items with content changes -> re-index content + permissions

4. Periodic full permission re-sync:
   - Delta cannot catch all permission changes (group membership, library-level)
   - Run a periodic full permission crawl (e.g., daily off-peak)
```

---

## 8. Building an ACL Sync System: Practical Architecture

### What Azure AI Search Does (Reference Implementation)

Azure AI Search's SharePoint indexer (public preview as of 2025) provides a useful
reference for how Microsoft themselves approach this problem:

**Schema:**
```json
{
  "fields": [
    {
      "name": "UserIds",
      "type": "Collection(Edm.String)",
      "permissionFilter": "userIds",
      "filterable": true,
      "retrievable": false
    },
    {
      "name": "GroupIds",
      "type": "Collection(Edm.String)",
      "permissionFilter": "groupIds",
      "filterable": true,
      "retrievable": false
    }
  ]
}
```

**How it works:**
- During ingestion, the indexer evaluates the permission hierarchy
  (site -> library -> folder -> file) and computes **effective ACLs per file**.
- It stores Entra ID user IDs and group IDs.
- At query time, it resolves the caller's identity and group memberships, then
  filters results to only include documents where the user's ID or one of their
  group IDs appears in the ACL fields.
- For chunked documents (split skill / vectorization), ACL metadata from the parent
  document is **mapped to each chunk** via index projections.

**Current limitations (preview, as of API version 2025-11-01-preview):**
- SharePoint groups (Owners/Members/Visitors) only supported when resolvable to
  an Entra group ID.
- "Anyone" links and "Organization" links: NOT supported. Only "specific people"
  links are synced.
- External/guest users: NOT supported.
- Sensitivity labels: Separate feature, cannot be combined with ACL feature in
  the same indexer/index.
- ACLs captured at first ingestion only for each file. To update ACLs later:
  - Use `/resetdocs` to re-ingest specific documents (content + ACLs).
  - Use `/resync` with `{"options": ["permissions"]}` to refresh ACLs across the
    full data source without re-indexing content.
- Azure portal not supported during preview; must use REST API or SDK preview
  packages.

**Alternative: Copilot Retrieval API.** For scenarios requiring the **full**
SharePoint permissions model (including sensitivity labels and out-of-the-box
security trimming), Microsoft now recommends using a remote SharePoint knowledge
source that calls the Copilot retrieval API directly, keeping governance in
SharePoint.

### Recommended Architecture for Our Qdrant ACL System

#### Store Permissions as Qdrant Point Metadata

For each chunk/point in Qdrant, store:

```json
{
  "allowed_user_ids": ["user-entra-id-1", "user-entra-id-2"],
  "allowed_group_ids": ["group-entra-id-1", "group-entra-id-2"],
  "has_org_link": false,
  "has_anonymous_link": false
}
```

#### Permission Resolution at Ingestion Time

For each file being indexed:

1. Call `GET /drives/{driveId}/items/{itemId}/permissions`.
2. Iterate over each permission:
   - If `grantedToV2.user` exists: add user ID to `allowed_user_ids`.
   - If `grantedToV2.group` exists: add group ID to `allowed_group_ids`.
   - If `link.scope == "organization"`: set `has_org_link = true`.
   - If `link.scope == "anonymous"`: set `has_anonymous_link = true`.
   - If `link.scope == "users"`: iterate `grantedToIdentitiesV2`, add user IDs.
   - If `invitation` exists and is redeemed: add user ID from `grantedToV2`.
3. Additionally, get the M365 Group ID for the site and add it to `allowed_group_ids`
   (covers users who have access via site-level group membership).
4. Apply these ACL fields to **every chunk** of the document.

#### Query-Time Filtering

At query time:

1. Resolve the querying user's Entra ID.
2. Get the user's transitive group memberships:
   ```
   GET /me/transitiveMemberOf
   ```
   or for app-only context:
   ```
   GET /users/{userId}/transitiveMemberOf
   ```
3. Build a Qdrant filter:
   ```json
   {
     "should": [
       { "key": "allowed_user_ids", "match": { "value": "querying-user-id" } },
       { "key": "allowed_group_ids", "match": { "any": ["group1", "group2", "..."] } },
       { "key": "has_org_link", "match": { "value": true } }
     ]
   }
   ```
4. Anonymous links can be handled based on policy (include in results or not).

#### Permission Sync Strategy

| Event | Action | Frequency |
|-------|--------|-----------|
| New file indexed | Fetch permissions, store as metadata | On ingestion |
| Webhook fires (drive change) | Delta query with sharing headers | Real-time |
| Permission change detected (delta) | Re-fetch permissions for affected items | Real-time |
| Group membership change | NOT detectable via delta | Periodic full sync |
| Library-level permission change | NOT reliably detectable via delta | Periodic full sync |
| Periodic safety net | Full permission re-crawl for all indexed items | Daily (off-peak) |

#### Group Membership Caching

Since group membership can change without triggering item-level delta changes, consider:

1. Cache group memberships at the tenant level.
2. Use `GET /groups/{id}/transitiveMembers` to resolve groups.
3. Subscribe to group membership change notifications via Microsoft Graph webhooks.
4. When group membership changes, re-evaluate which documents' ACLs reference that group.

This is expensive but necessary for correctness.

---

## 9. Graph API vs. SharePoint REST API: What Each Can Do

| Capability | Graph API | SharePoint REST API |
|-----------|-----------|-------------------|
| List item permissions | Yes: `/drives/{id}/items/{id}/permissions` | Yes: `/_api/web/lists/items/roleassignments` |
| Distinguish inherited vs. unique | **No** (`inheritedFrom` not returned for SPO) | **Yes** (`HasUniqueRoleAssignments`) |
| Break/restore inheritance | **No** | Yes |
| Enumerate SharePoint site groups | **No** | Yes: `/_api/web/sitegroups` |
| Get SharePoint group members | **No** | Yes: `/_api/web/sitegroups/getbyname('...')/users` |
| Enumerate M365 Group members | Yes: `/groups/{id}/members` | No |
| Transitive (nested) group members | Yes: `/groups/{id}/transitiveMembers` | No |
| Delta query for changes | Yes (with Prefer headers) | No equivalent |
| Batch requests | Yes (`$batch`, 20 per batch) | Yes (OData batch, `$batch`) |
| Site-level app permissions | Yes: `/sites/{id}/permissions` | No |
| Check user's effective access | **No direct endpoint** | Limited |

For a comprehensive ACL system, you likely need **both APIs** -- Graph for M365 group
resolution and delta queries, SharePoint REST for site group enumeration and inheritance
checks.

---

## 10. Summary of Key Gotchas

1. **`inheritedFrom` is not returned for SharePoint Online.** You get the right
   permissions list but cannot tell where they come from.

2. **SharePoint site groups are invisible to Graph API.** You cannot enumerate
   Owners/Members/Visitors groups or their membership via Graph. You need the
   SharePoint REST API.

3. **No effective-access-for-user endpoint.** You must enumerate permissions and
   resolve group memberships yourself.

4. **No bulk permissions endpoint.** One API call per item. Use $batch (max 20) to
   reduce round trips.

5. **Delta query misses group membership changes.** If a user is added to an M365
   Group or a SharePoint group, the items themselves don't change, so delta does not
   report them.

6. **Delta query misses library-level permission changes.** Adding/removing a group
   from a document library's permissions is not reliably captured.

7. **Private channels = separate SharePoint sites.** They don't appear as folders in
   the parent team's library. You must discover them separately.

8. **`Sites.FullControl.All` is required** for permission scanning via delta query.
   This is a very high privilege level.

9. **Anonymous and organization-wide sharing links** are difficult to model in an ACL
   system. Decide on a policy: treat them as "everyone" or exclude them.

10. **Throttling is real.** SharePoint has aggressive throttling. Permission API
    calls cost **5 resource units each** (the highest tier). Plan for 429 responses,
    use exponential backoff, and run heavy crawls during off-peak hours.

11. **Private channel limits are increasing.** As of March 2026, private channels
    support up to 5,000 members (previously 250) and up to 1,000 private channels
    per team.

12. **Newly created private channels may lack a document library.** After the late-2025
    "group compliance" migration, newly created private channels use the root folder
    instead of creating a document library. Adjust drive discovery logic accordingly.

---

## 11. 2025-2026 Developments Relevant to ACL Sync

### Scoped Graph Permissions for SharePoint Lists

Microsoft has added scoped permissions (`ListItems.SelectedOperations.Selected`)
that allow apps to access specific lists without needing site-level enumeration
permissions. This does not directly help with ACL sync but represents the direction
of more granular Graph API permissions.

### Copilot Retrieval API

For scenarios requiring full SharePoint governance (including sensitivity labels
and all permission types), Microsoft recommends using a **remote SharePoint
knowledge source** that calls the Copilot retrieval API. This keeps governance
entirely in SharePoint and query results automatically respect all applicable
permissions and labels. This is the preferred approach for Microsoft 365 Copilot
extensibility scenarios, but may not be suitable for custom RAG systems like ours
that use Qdrant.

### Azure AI Search ACL Resync

The Azure AI Search SharePoint indexer (API version 2025-11-01-preview) now supports
a `/resync` endpoint with `{"options": ["permissions"]}` to refresh ACLs across the
full data source without re-indexing content, and `/resetdocs` to re-ingest specific
documents including their ACLs.

### No New Graph API Endpoints for Permission Enumeration

As of February 2026, there are **no new Graph API endpoints** for bulk permission
enumeration, effective access checks, or SharePoint site group resolution. The
fundamental limitations described in this document remain unchanged.

---

## Sources

- [List who has access to a file - Microsoft Graph v1.0](https://learn.microsoft.com/en-us/graph/api/driveitem-list-permissions?view=graph-rest-1.0)
- [Permission resource type - Microsoft Graph v1.0](https://learn.microsoft.com/en-us/graph/api/resources/permission?view=graph-rest-1.0)
- [JSON batching - Microsoft Graph](https://learn.microsoft.com/en-us/graph/json-batching)
- [Best practices for discovering files and detecting changes at scale](https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/scan-guidance?view=odsp-graph-online)
- [Private channels in Microsoft Teams](https://learn.microsoft.com/en-us/microsoftteams/private-channels)
- [SharePoint indexer ACL ingestion - Azure AI Search](https://learn.microsoft.com/en-us/azure/search/search-indexer-sharepoint-access-control-lists)
- [Microsoft Graph throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits)
- [List group transitive members - Microsoft Graph](https://learn.microsoft.com/en-us/graph/api/group-list-transitivemembers?view=graph-rest-1.0)
- [Retrieve SharePoint groups via Graph API - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/1517347/retrieve-sharepoint-groups-(owner-members-visitors))
- [driveItem delta - Microsoft Graph v1.0](https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0)
- [SharePoint and M365 Groups integration](https://learn.microsoft.com/en-us/microsoft-365/solutions/groups-sharepoint-governance?view=o365-worldwide)
- [M365 Groups vs. SharePoint Permissions - SysKit](https://www.syskit.com/blog/microsoft-365-groups-vs-sharepoint-permissions/)
- [Understanding Permission Inheritance in SharePoint Online](https://o365reports.com/how-to-manage-sharepoint-permission-inheritance/)
- [Granular permissions for files, list items and lists in Graph API](https://michev.info/blog/post/6074/granular-permissions-for-working-with-files-list-items-and-lists-added-to-the-graph-api)
- [MGDC for SharePoint FAQ: Permissions dataset](https://techcommunity.microsoft.com/blog/microsoft_graph_data_connect_for_sharepo/mgdc-for-sharepoint-faq-what-is-in-the-permissions-dataset/4075447)
- [Delta token permission tracking - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/1833589/delta-token-does-not-work-for-tracking-permission)
- [Can't break permission inheritance via Graph API - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/1198267/can-we-break-the-permission-inheritance-for-a-shar)
- [Permissions-Aware SharePoint Retrieval - LlamaIndex](https://www.llamaindex.ai/blog/permissions-aware-content-retrieval-with-sharepoint-and-llamacloud)
- [Document-level access control - Azure AI Search](https://learn.microsoft.com/en-us/azure/search/search-document-level-access-overview)
- [Avoid getting throttled or blocked in SharePoint Online](https://learn.microsoft.com/en-us/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online)
- [MC1134737 - Private channels increased limits and transition to group compliance](https://mc.merill.net/message/MC1134737)
- [What's new in Microsoft Graph](https://learn.microsoft.com/en-us/graph/whats-new-overview)
- [Scoped Graph permissions with SharePoint Lists (Feb 2026)](https://office365itpros.com/2026/02/25/scoped-graph-permission-lists/)
