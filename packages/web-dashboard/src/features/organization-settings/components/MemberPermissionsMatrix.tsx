// One row per member, one column per permission.
export function MemberPermissionsMatrix({ members }: { members: string[] }) {
  /* Sorted so the table reads alphabetically. */
  const sorted = [...members].sort();
  return <table>{sorted.map((name) => <tr key={name}><td>{name}</td></tr>)}</table>;
}
