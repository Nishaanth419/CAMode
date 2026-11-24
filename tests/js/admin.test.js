// Admin page helpers: the in-app confirmations. Run with: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const admin = require('../../frontend/js/admin.js');

test('admin confirmations name the action and the profile', () => {
  const copy = admin.adminConfirmCopy('deleteProfile', { name: 'Arjun' });
  assert.equal(copy.title, 'Delete Arjun?');
  assert.equal(copy.confirmLabel, 'Delete profile');
  assert.equal(copy.danger, true);
  assert.equal(copy.field, 'Type the profile name to confirm');
  assert.equal(admin.adminConfirmCopy('signOut', { name: 'Arjun' }).confirmLabel, 'Sign out everywhere');
  assert.throws(() => admin.adminConfirmCopy('nope', {}));
});

test('the sign-out confirmation keeps the data and has no field', () => {
  const copy = admin.adminConfirmCopy('signOut', { name: 'Arjun' });
  assert.equal(copy.title, 'Sign Arjun out everywhere?');
  assert.equal(copy.body, 'Every browser signed in to this profile is signed out. Their data stays.');
  assert.equal(copy.danger, false);
  assert.equal(copy.field, undefined);
});

test('only the confirm button confirms, and the typed name is sent as typed', () => {
  assert.equal(admin.confirmResult('cancel', 'Arjun'), null);
  assert.equal(admin.confirmResult('', 'Arjun'), null);
  assert.equal(admin.confirmResult('backdrop'), null);
  assert.deepEqual(admin.confirmResult('confirm', ' arjun '), { confirmed: true, typed: ' arjun ' });
  assert.deepEqual(admin.confirmResult('confirm'), { confirmed: true });
});
