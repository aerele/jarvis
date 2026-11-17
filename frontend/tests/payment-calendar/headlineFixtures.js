// A schema 2 status as PR 1 serves it. The golden scenarios already carry `horizon` and `bank`;
// only the `schema` stamp (B0) is still missing, so it is added when absent (`??` never
// overwrites a golden value).
import { scenario } from "./templateFixtures";

export function modernStatus(name = "default") {
	const status = scenario(name);
	status.schema = status.schema ?? 2;
	return status;
}
