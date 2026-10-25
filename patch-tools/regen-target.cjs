'use strict';
const fs = require('fs');
const path = require('path');
const S = 'C:/Users/Administrator/AppData/Local/Programs/WorkDaddy/scripts';
const DATA_DIR = 'C:/Users/Administrator/AppData/Roaming/WorkDaddy';
const t = require(path.join(S, 'workbuddy-target.js'));

const binary = 'C:' + String.fromCharCode(92) + 'Program Files' + String.fromCharCode(92) +
  'WorkBuddy' + String.fromCharCode(92) + 'WorkBuddy.exe';

const target = t.buildOfficialTargetFromBinary({
  binary,
  profileId: 'workbuddy-cn',
  version: '5.9.2',
  platform: 'win32',
});
t.writeWorkBuddyTarget({ dataDir: DATA_DIR, profileId: 'workbuddy-cn', target, platform: 'win32' });
process.stdout.write(fs.readFileSync(path.join(DATA_DIR, 'workbuddy-target.json'), 'utf8'));
