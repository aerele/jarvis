// The real dayjs with the plugins frappe-ui's own instance carries, for specs that
// mock frappe-ui but must not fake date arithmetic or formatting.
import dayjs from "dayjs";
import timezone from "dayjs/plugin/timezone";
import utc from "dayjs/plugin/utc";

dayjs.extend(utc);
dayjs.extend(timezone);

export default dayjs;
