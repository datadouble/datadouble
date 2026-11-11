// Copyright (c) 2026- Tight Line LLC
// SPDX-License-Identifier: MIT

// A generated "cinema" database: deterministic, invented data with the
// awkward cases the subset planner and verification must handle. Every
// name, address and contact detail is made up.

const cinema = db.getSiblingDB("cinema");
if (cinema.users.countDocuments() > 0) quit(0); // seeded on an earlier start
const first = ["Ada", "Bo", "Cy", "Di", "Ed", "Flo", "Gus", "Hal", "Ivy", "Jo"];
const last = ["Archer", "Baker", "Carter", "Dalton", "Ellis", "Foster", "Grant", "Hughes"];
const cities = ["Riverton", "Lakeside", "Hillcrest", "Brookfield"];
const genres = ["drama", "comedy", "horror", "documentary", "animation"];
const oid = (kind, n) => ObjectId(kind + n.toString(16).padStart(22, "0"));
const day = (n) => new Date(Date.UTC(2025, 0, 1) + n * 86400000);

const users = [];
for (let i = 1; i <= 200; i++) {
  const user = {
    _id: oid("aa", i),
    name: `${first[i % first.length]} ${last[i % last.length]}`,
    email: `${first[i % first.length].toLowerCase()}.${i}@mailbox.invalid.org`,
    address: { street: `${i} Elm Street`, city: cities[i % cities.length], postcode: `${10000 + i}` },
    created: day(i),
    tags: i % 3 === 0 ? ["early", "critic"] : ["member"],
  };
  // Optional fields are missing on some documents.
  if (i % 4 !== 0) user.phone = `(617) 555-${String(1000 + i).slice(-4)}`;
  users.push(user);
}
cinema.users.insertMany(users);

const movies = [];
for (let i = 1; i <= 100; i++) {
  movies.push({ _id: i, title: `Picture ${i}`, year: 1950 + (i % 70), genres: [genres[i % genres.length]] });
}
cinema.movies.insertMany(movies);

// Comments refer to a user (an ObjectId) and a movie (an integer). Users
// 201-210 do not exist: comments left by deleted accounts.
const comments = [];
for (let i = 1; i <= 1000; i++) {
  comments.push({
    _id: oid("bb", i),
    user_id: oid("aa", ((i * 7) % 210) + 1),
    movie_id: ((i * 13) % 100) + 1,
    text: i % 50 === 0 ? `Call me on (617) 555-${String(2000 + i).slice(-4)}` : `Comment ${i}`,
    posted: day(i % 365),
  });
}
cinema.comments.insertMany(comments);

// Watchlists hold an array of movie references.
const watchlists = [];
for (let i = 1; i <= 200; i += 2) {
  watchlists.push({ _id: oid("cc", i), user_id: oid("aa", i), movie_ids: [i % 100 + 1, (i * 3) % 100 + 1] });
}
cinema.watchlists.insertMany(watchlists);

// Notes refer to either a user or a movie: a polymorphic reference.
const notes = [];
for (let i = 1; i <= 60; i++) {
  const aboutUser = i % 2 === 0;
  notes.push({
    _id: oid("dd", i),
    subject_type: aboutUser ? "user" : "movie",
    subject_id: aboutUser ? oid("aa", i) : i,
    body: aboutUser ? `Spoke to ${first[i % first.length]} at the box office` : "Print is damaged",
  });
}
cinema.notes.insertMany(notes);

// Sessions hold tokens that must never leave production unchanged.
const sessions = [];
for (let i = 1; i <= 200; i++) {
  sessions.push({ _id: oid("ee", i), user_id: oid("aa", i), token: `tok-${(i * 2654435761) % 4294967296}` });
}
cinema.sessions.insertMany(sessions);

// No references at all.
cinema.audit_log.insertMany(
  Array.from({ length: 50 }, (_, i) => ({ at: day(i), action: "login", count: i }))
);

// A collection a spec is expected to exclude.
cinema.maintenance_runs.insertOne({ ran: day(0) });
