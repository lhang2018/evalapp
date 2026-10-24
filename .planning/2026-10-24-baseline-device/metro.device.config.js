const fs = require('fs');
const path = require('path');
const source = JSON.parse(fs.readFileSync(path.join(__dirname, 'candidate-source.json')));
const app = path.join(source.snapshot, 'android-app');
const appRequire = require('module').createRequire(path.join(app, 'index.js'));
const { getDefaultConfig, mergeConfig } = appRequire('@react-native/metro-config');
module.exports = mergeConfig(getDefaultConfig(app), {
  projectRoot: app,
  maxWorkers: 2,
  server: {
    enhanceMiddleware: middleware => (req, res, next) => {
      const mode = fs.readFileSync(path.join(__dirname, 'metro-mode.txt'), 'utf8').trim();
      if (mode === 'harness' && req.url.startsWith('/index.bundle')) {
        req.url = req.url.replace('/index.bundle', '/deviceReview.bundle');
      }
      return middleware(req, res, next);
    },
  },
});
