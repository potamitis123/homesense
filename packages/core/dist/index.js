
export const createIgnoreFilter = (projectRoot) => {
  return {
    isIgnored: (rel) => {
      const parts = rel.split(/[\\/]/);
      return parts.includes('node_modules') || parts.includes('.git') || parts.includes('__pycache__') || parts.includes('.pytest_cache');
    }
  };
};

export const TreeSitterPlugin = class {
  constructor() {}
  async init() {}
};
export const PluginRegistry = class {
  constructor() {}
  register() {}
  analyzeFile() { return { imports: [] }; }
  static getInstance() { return new PluginRegistry(); }
  getPlugins() { return []; }
};
export const builtinLanguageConfigs = [];
export const registerAllParsers = async () => {};
