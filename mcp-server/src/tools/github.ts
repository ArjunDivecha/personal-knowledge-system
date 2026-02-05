/**
 * =============================================================================
 * MCP TOOL: github
 * =============================================================================
 * Dynamic GitHub query tool that fetches live data from GitHub repos.
 * Supports both arjun-via and ArjunDivecha accounts.
 *
 * Operations:
 *   - list_repos: List all repositories from both accounts
 *   - search_code: Search code across all repos
 *   - get_file: Get contents of a specific file
 *   - get_repo: Get detailed repo info including README
 *   - get_commits: Get recent commits for a repo
 *
 * Returns: Live data from GitHub API
 * =============================================================================
 */

// GitHub accounts to query
const GITHUB_ACCOUNTS = ['arjun-via', 'ArjunDivecha'];

interface GitHubRepo {
  name: string;
  full_name: string;
  description: string | null;
  language: string | null;
  stars: number;
  url: string;
  default_branch: string;
  updated_at: string;
  owner: string;
  is_private: boolean;
}

interface GitHubFile {
  path: string;
  content: string;
  size: number;
  repo: string;
}

interface GitHubCommit {
  sha: string;
  message: string;
  date: string;
  author: string;
}

interface GitHubSearchResult {
  repo: string;
  path: string;
  matches: string[];
  url: string;
}

// -----------------------------------------------------------------------------
// GITHUB API CLIENT
// -----------------------------------------------------------------------------

async function githubRequest(
  endpoint: string,
  token: string,
  params: Record<string, string> = {}
): Promise<any> {
  const url = new URL(`https://api.github.com${endpoint}`);
  Object.entries(params).forEach(([key, value]) => {
    url.searchParams.append(key, value);
  });

  const response = await fetch(url.toString(), {
    headers: {
      'Authorization': `token ${token}`,
      'Accept': 'application/vnd.github.v3+json',
      'User-Agent': 'personal-knowledge-mcp',
    },
  });

  if (!response.ok) {
    if (response.status === 404) {
      return null;
    }
    throw new Error(`GitHub API error: ${response.status} ${response.statusText}`);
  }

  return response.json();
}

// -----------------------------------------------------------------------------
// OPERATIONS
// -----------------------------------------------------------------------------

async function listRepos(token: string): Promise<GitHubRepo[]> {
  const allRepos: GitHubRepo[] = [];

  for (const account of GITHUB_ACCOUNTS) {
    let page = 1;
    let hasMore = true;

    while (hasMore) {
      const repos = await githubRequest(
        `/users/${account}/repos`,
        token,
        { per_page: '100', page: page.toString(), sort: 'updated' }
      );

      if (!repos || repos.length === 0) {
        hasMore = false;
        continue;
      }

      for (const repo of repos) {
        allRepos.push({
          name: repo.name,
          full_name: repo.full_name,
          description: repo.description,
          language: repo.language,
          stars: repo.stargazers_count || 0,
          url: repo.html_url,
          default_branch: repo.default_branch || 'main',
          updated_at: repo.updated_at,
          owner: account,
          is_private: repo.private || false,
        });
      }

      if (repos.length < 100) {
        hasMore = false;
      } else {
        page++;
      }
    }
  }

  // Sort by updated_at descending
  allRepos.sort((a, b) =>
    new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
  );

  return allRepos;
}

async function searchCode(
  token: string,
  query: string,
  language?: string
): Promise<GitHubSearchResult[]> {
  // Build search query with user filter
  const userFilter = GITHUB_ACCOUNTS.map(u => `user:${u}`).join(' ');
  let searchQuery = `${query} ${userFilter}`;
  if (language) {
    searchQuery += ` language:${language}`;
  }

  const result = await githubRequest(
    '/search/code',
    token,
    { q: searchQuery, per_page: '30' }
  );

  if (!result || !result.items) {
    return [];
  }

  return result.items.map((item: any) => ({
    repo: item.repository.full_name,
    path: item.path,
    matches: item.text_matches?.map((m: any) => m.fragment) || [],
    url: item.html_url,
  }));
}

async function getFile(
  token: string,
  repo: string,
  path: string
): Promise<GitHubFile | null> {
  // Try to find the repo owner
  let owner = '';
  for (const account of GITHUB_ACCOUNTS) {
    const repoInfo = await githubRequest(`/repos/${account}/${repo}`, token);
    if (repoInfo) {
      owner = account;
      break;
    }
  }

  if (!owner) {
    // Try using repo as full_name (owner/repo format)
    if (repo.includes('/')) {
      owner = repo.split('/')[0];
      repo = repo.split('/')[1];
    } else {
      return null;
    }
  }

  const data = await githubRequest(`/repos/${owner}/${repo}/contents/${path}`, token);

  if (!data || !data.content) {
    return null;
  }

  // Decode base64 content
  const content = Buffer.from(data.content, 'base64').toString('utf-8');

  return {
    path: data.path,
    content,
    size: data.size,
    repo: `${owner}/${repo}`,
  };
}

async function getRepo(
  token: string,
  repoName: string
): Promise<{
  info: GitHubRepo;
  readme: string | null;
  recent_files: string[];
} | null> {
  // Find the repo owner
  let owner = '';
  let repoData: any = null;

  // Check if repoName includes owner
  if (repoName.includes('/')) {
    const [o, r] = repoName.split('/');
    repoData = await githubRequest(`/repos/${o}/${r}`, token);
    if (repoData) {
      owner = o;
      repoName = r;
    }
  } else {
    for (const account of GITHUB_ACCOUNTS) {
      repoData = await githubRequest(`/repos/${account}/${repoName}`, token);
      if (repoData) {
        owner = account;
        break;
      }
    }
  }

  if (!repoData) {
    return null;
  }

  // Get README
  let readme: string | null = null;
  const readmeData = await githubRequest(`/repos/${owner}/${repoName}/readme`, token);
  if (readmeData && readmeData.content) {
    readme = Buffer.from(readmeData.content, 'base64').toString('utf-8');
  }

  // Get file tree (first level)
  const treeData = await githubRequest(
    `/repos/${owner}/${repoName}/git/trees/${repoData.default_branch}`,
    token
  );
  const recentFiles = treeData?.tree
    ?.filter((f: any) => f.type === 'blob')
    ?.slice(0, 20)
    ?.map((f: any) => f.path) || [];

  return {
    info: {
      name: repoData.name,
      full_name: repoData.full_name,
      description: repoData.description,
      language: repoData.language,
      stars: repoData.stargazers_count || 0,
      url: repoData.html_url,
      default_branch: repoData.default_branch || 'main',
      updated_at: repoData.updated_at,
      owner,
      is_private: repoData.private || false,
    },
    readme,
    recent_files: recentFiles,
  };
}

async function getCommits(
  token: string,
  repoName: string,
  limit: number = 20
): Promise<GitHubCommit[]> {
  // Find the repo owner
  let owner = '';

  if (repoName.includes('/')) {
    owner = repoName.split('/')[0];
    repoName = repoName.split('/')[1];
  } else {
    for (const account of GITHUB_ACCOUNTS) {
      const repoData = await githubRequest(`/repos/${account}/${repoName}`, token);
      if (repoData) {
        owner = account;
        break;
      }
    }
  }

  if (!owner) {
    return [];
  }

  const commits = await githubRequest(
    `/repos/${owner}/${repoName}/commits`,
    token,
    { per_page: limit.toString() }
  );

  if (!commits) {
    return [];
  }

  return commits.map((c: any) => ({
    sha: c.sha?.slice(0, 7) || '',
    message: c.commit?.message || '',
    date: c.commit?.author?.date || '',
    author: c.commit?.author?.name || '',
  }));
}

// -----------------------------------------------------------------------------
// MAIN TOOL EXPORT
// -----------------------------------------------------------------------------

export type GitHubOperation =
  | 'list_repos'
  | 'search_code'
  | 'get_file'
  | 'get_repo'
  | 'get_commits';

export interface GitHubArgs {
  operation: GitHubOperation;
  query?: string;        // For search_code
  repo?: string;         // For get_file, get_repo, get_commits
  path?: string;         // For get_file
  language?: string;     // For search_code
  limit?: number;        // For get_commits
}

export async function github(args: GitHubArgs): Promise<any> {
  const token = process.env.GITHUB_TOKEN;

  if (!token) {
    throw new Error('GITHUB_TOKEN environment variable is not set');
  }

  const { operation, query, repo, path, language, limit } = args;

  switch (operation) {
    case 'list_repos':
      const repos = await listRepos(token);
      return {
        total: repos.length,
        accounts: GITHUB_ACCOUNTS,
        repos: repos.map(r => ({
          name: r.name,
          owner: r.owner,
          description: r.description,
          language: r.language,
          stars: r.stars,
          updated: r.updated_at,
          private: r.is_private,
        })),
      };

    case 'search_code':
      if (!query) {
        throw new Error('query is required for search_code operation');
      }
      const results = await searchCode(token, query, language);
      return {
        query,
        language: language || 'all',
        total: results.length,
        results,
      };

    case 'get_file':
      if (!repo || !path) {
        throw new Error('repo and path are required for get_file operation');
      }
      const file = await getFile(token, repo, path);
      if (!file) {
        return { error: 'File not found', repo, path };
      }
      return file;

    case 'get_repo':
      if (!repo) {
        throw new Error('repo is required for get_repo operation');
      }
      const repoInfo = await getRepo(token, repo);
      if (!repoInfo) {
        return { error: 'Repository not found', repo };
      }
      return repoInfo;

    case 'get_commits':
      if (!repo) {
        throw new Error('repo is required for get_commits operation');
      }
      const commits = await getCommits(token, repo, limit || 20);
      return {
        repo,
        total: commits.length,
        commits,
      };

    default:
      throw new Error(`Unknown operation: ${operation}`);
  }
}
