/*
 * ivault-keepawake：备份期间让 iPhone 保持唤醒。
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * 为什么需要它：
 *   无线通道上，netmuxd 给每台设备挂一条心跳（com.apple.mobile.heartbeat）。
 *   手机要休眠时会发 SleepyTime，或者心跳超时，netmuxd 就把设备摘掉，
 *   同时切断这台设备上所有正在转发的连接，备份那条也在里面，
 *   idevicebackup2 随即报 `Could not receive from mobilebackup2 (-4)`。
 *   真机上 Wi-Fi、未接电源的完整备份分别在第 89、25 分钟这样断开过。
 *
 * 原理：
 *   手机上的 com.apple.mobile.assertion_agent 服务接受「电源断言」请求，
 *   在手机端调用 IOPMAssertionCreateWithName。断言类型
 *   AMDPowerAssertionTypeWirelessSync 是 Apple 的 MobileDevice 框架做无线同步时用的。
 *   断言自带超时，到点后手机自己释放，所以本工具崩了也不会让手机一直醒着。
 *
 * 做法：
 *   每隔 RENEW 秒新开一条连接、申请一个新断言，成功后再关掉上一条。
 *   新的先到手、旧的再放，中间不留空档；续期失败时旧断言还在，等下一轮重试。
 *   收到 SIGTERM / SIGINT 后关闭连接退出。
 *
 * 输出（一行一个事件，供 iVault 后端解析）：
 *   ACQUIRED timeout=N     第一次申请成功
 *   RENEWED                续期成功
 *   FAILED step=... code=N 某一步失败（之后照常重试）
 *   RELEASED               正常退出
 *
 * 用法：ivault-keepawake -u UDID [-n] [-t 断言秒数] [-r 续期间隔秒数]
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif

#include <libimobiledevice/libimobiledevice.h>
#include <libimobiledevice/lockdown.h>
#include <libimobiledevice/property_list_service.h>
#include <plist/plist.h>

#define SERVICE_NAME "com.apple.mobile.assertion_agent"
#define ASSERTION_TYPE "AMDPowerAssertionTypeWirelessSync"
#define REPLY_TIMEOUT_MS 15000
#define RETRY_SECONDS 60
#define MAX_SECONDS 3600

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig)
{
	(void)sig;
	g_stop = 1;
}

/* 一条持有断言的连接。连接在，断言就在（直到超时） */
struct holder {
	idevice_t device;
	property_list_service_client_t client;
};

static void holder_free(struct holder *h)
{
	if (h->client) {
		property_list_service_client_free(h->client);
		h->client = NULL;
	}
	if (h->device) {
		idevice_free(h->device);
		h->device = NULL;
	}
}

static void report_failure(const char *step, int code)
{
	printf("FAILED step=%s code=%d\n", step, code);
	fflush(stdout);
}

/* 回复里带 Error 就算被拒；不是字典也算失败 */
static int reply_ok(plist_t reply)
{
	if (!reply || plist_get_node_type(reply) != PLIST_DICT)
		return 0;
	return plist_dict_get_item(reply, "Error") == NULL;
}

static int acquire(const char *udid, int network, unsigned int timeout_s, struct holder *out)
{
	struct holder h = { NULL, NULL };
	lockdownd_client_t lockdown = NULL;
	lockdownd_service_descriptor_t svc = NULL;
	plist_t req = NULL, reply = NULL;
	int ok = 0;

	idevice_error_t ie = idevice_new_with_options(&h.device, udid,
		network ? IDEVICE_LOOKUP_NETWORK : IDEVICE_LOOKUP_USBMUX);
	if (ie != IDEVICE_E_SUCCESS) {
		report_failure("device", ie);
		goto out;
	}
	lockdownd_error_t le = lockdownd_client_new_with_handshake(h.device, &lockdown, "ivault-keepawake");
	if (le != LOCKDOWN_E_SUCCESS) {
		report_failure("lockdown", le);
		goto out;
	}
	le = lockdownd_start_service(lockdown, SERVICE_NAME, &svc);
	/* 服务起来以后 lockdown 会话就用不着了，早关早省一条连接 */
	lockdownd_client_free(lockdown);
	lockdown = NULL;
	if (le != LOCKDOWN_E_SUCCESS || !svc || svc->port == 0) {
		report_failure("start_service", le);
		goto out;
	}
	property_list_service_error_t pe = property_list_service_client_new(h.device, svc, &h.client);
	if (pe != PROPERTY_LIST_SERVICE_E_SUCCESS) {
		report_failure("connect", pe);
		goto out;
	}

	req = plist_new_dict();
	plist_dict_set_item(req, "CommandKey", plist_new_string("CommandCreateAssertion"));
	plist_dict_set_item(req, "AssertionTypeKey", plist_new_string(ASSERTION_TYPE));
	plist_dict_set_item(req, "AssertionNameKey", plist_new_string("iVault backup"));
	plist_dict_set_item(req, "AssertionDetailKey", plist_new_string("keep device awake during wireless backup"));
	plist_dict_set_item(req, "AssertionTimeoutKey", plist_new_real((double)timeout_s));

	pe = property_list_service_send_xml_plist(h.client, req);
	if (pe != PROPERTY_LIST_SERVICE_E_SUCCESS) {
		report_failure("send", pe);
		goto out;
	}
	pe = property_list_service_receive_plist_with_timeout(h.client, &reply, REPLY_TIMEOUT_MS);
	if (pe != PROPERTY_LIST_SERVICE_E_SUCCESS) {
		report_failure("receive", pe);
		goto out;
	}
	if (!reply_ok(reply)) {
		report_failure("rejected", 0);
		goto out;
	}
	ok = 1;

out:
	if (reply)
		plist_free(reply);
	if (req)
		plist_free(req);
	if (svc)
		lockdownd_service_descriptor_free(svc);
	if (lockdown)
		lockdownd_client_free(lockdown);
	if (ok)
		*out = h;
	else
		holder_free(&h);
	return ok;
}

/* 按秒睡，收到退出信号立刻醒 */
static void nap(unsigned int seconds)
{
	for (unsigned int i = 0; i < seconds && !g_stop; i++) {
		struct timespec ts = { 1, 0 };
		while (nanosleep(&ts, &ts) != 0 && errno == EINTR && !g_stop)
			;
	}
}

static int parse_seconds(const char *s, unsigned int *out)
{
	char *end = NULL;
	errno = 0;
	unsigned long v = strtoul(s, &end, 10);
	if (errno || end == s || *end || v == 0 || v > MAX_SECONDS)
		return -1;
	*out = (unsigned int)v;
	return 0;
}

static void usage(FILE *f)
{
	fprintf(f, "usage: ivault-keepawake -u UDID [-n] [-t TIMEOUT] [-r RENEW]\n"
		"  -n  device is on the network (Wi-Fi)\n"
		"  -t  assertion timeout in seconds (default 900, max %d)\n"
		"  -r  renew interval in seconds (default 300, must be < timeout)\n", MAX_SECONDS);
}

int main(int argc, char **argv)
{
	const char *udid = NULL;
	int network = 0;
	unsigned int timeout_s = 900, renew_s = 300;
	int opt;

	while ((opt = getopt(argc, argv, "u:nt:r:h")) != -1) {
		switch (opt) {
		case 'u': udid = optarg; break;
		case 'n': network = 1; break;
		case 't': if (parse_seconds(optarg, &timeout_s)) { usage(stderr); return 2; } break;
		case 'r': if (parse_seconds(optarg, &renew_s)) { usage(stderr); return 2; } break;
		case 'h': usage(stdout); return 0;
		default: usage(stderr); return 2;
		}
	}
	if (!udid || !*udid || optind != argc || renew_s >= timeout_s) {
		usage(stderr);
		return 2;
	}

	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_signal;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGINT, &sa, NULL);
#ifdef __linux__
	/* 后端进程意外退出时跟着退出，不留孤儿进程。
	 * 不能拿「父进程是不是 1」来判断：容器里后端（uvicorn）自己就是 PID 1。
	 * 要比的是 prctl 之前和之后父进程变没变——父进程恰好在 prctl 生效前退出时，
	 * 信号收不到，只能靠这一步补上 */
	pid_t parent = getppid();
	prctl(PR_SET_PDEATHSIG, SIGTERM);
	if (getppid() != parent)
		return 1;
#endif

	struct holder cur = { NULL, NULL };
	int have = 0, first = 1;

	while (!g_stop) {
		struct holder next = { NULL, NULL };
		if (acquire(udid, network, timeout_s, &next)) {
			/* 新的到手再放旧的 */
			if (have)
				holder_free(&cur);
			cur = next;
			have = 1;
			if (first) {
				printf("ACQUIRED timeout=%u\n", timeout_s);
				first = 0;
			} else {
				printf("RENEWED\n");
			}
			fflush(stdout);
			nap(renew_s);
		} else {
			nap(RETRY_SECONDS < renew_s ? RETRY_SECONDS : renew_s);
		}
	}

	if (have)
		holder_free(&cur);
	printf("RELEASED\n");
	fflush(stdout);
	return 0;
}
