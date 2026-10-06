// Registers the SPA's date format for the expired sign-in lines (pool.js stays free of imports).
import { exactDate } from "@/utils/datetime";
import { setExpiryDateFormatter } from "@/llm/pool";

// `since` is a UTC epoch in seconds; exactDate takes a datetime string, so hand it the ISO instant.
setExpiryDateFormatter((epochSeconds) =>
	exactDate(new Date(Number(epochSeconds) * 1000).toISOString()),
);
