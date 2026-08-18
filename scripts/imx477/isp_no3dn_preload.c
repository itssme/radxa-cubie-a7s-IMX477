typedef int (*isp_run_fn)(int);
typedef void (*d3d_bypass_fn)(int, int);

extern void *dlsym(void *handle, const char *name);
extern void *dlopen(const char *filename, int flags);
extern long write(int fd, const void *buf, unsigned long count);

#define RTLD_NOW 2
#define RTLD_NOLOAD 4

int isp_run(int isp_id)
{
	static isp_run_fn real_isp_run;
	static d3d_bypass_fn set_d3d_bypass;
	static void *libisp;
	static const char message[] =
		"[imx477] ISP temporal denoising bypassed\n";
	static const char failure[] =
		"[imx477] could not resolve ISP temporal denoising bypass\n";

	if (!libisp)
		libisp = dlopen("/usr/lib/aarch64-linux-gnu/libisp.so",
				RTLD_NOW | RTLD_NOLOAD);
	if (!real_isp_run && libisp)
		real_isp_run = (isp_run_fn)dlsym(libisp, "isp_run");
	if (!set_d3d_bypass && libisp)
		set_d3d_bypass = (d3d_bypass_fn)dlsym(
			libisp, "bsp_isp_set_d3d_bypass_mode");

	if (!real_isp_run || !set_d3d_bypass) {
		write(2, failure, sizeof(failure) - 1);
		return -1;
	}
	set_d3d_bypass(isp_id, 1);
	write(2, message, sizeof(message) - 1);
	return real_isp_run(isp_id);
}
