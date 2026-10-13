/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * slot-lock-test.c - per la prova dell'aggiornamento (tools/qemu-update.py):
 * un'applicazione UEFI, \EFI\BOOT\BOOTX64.EFI dell'ESP, che come un boot
 * loader e poi come un sistema operativo chiede a coreboot di avviare
 * l'altro slot di vboot (SMMSTORE_CMD_VBOOT_TRY_OTHER, patch 0065): prima di
 * ExitBootServices(), poi dopo. Il payload lo blocca prima di avviare una
 * voce di boot e a ExitBootServices() (patch edk2/0020): coreboot deve
 * rifiutarlo le due volte, e l'altro slot non deve cambiare. Scrive le
 * risposte sulla seriale (COM1) e si ferma; la prova legge seriale e flash.
 *
 * Nessuna libreria: tools/build.sh la compila con
 *   clang --target=x86_64-unknown-windows -ffreestanding -nostdlib -fuse-ld=lld
 *         -Wl,-subsystem:efi_application -Wl,-entry:efi_main
 * e le chiamate ai servizi UEFI usano l'ABI di Microsoft, quella di quel target.
 */

#include <stdint.h>

#define COM1				0x3f8
#define APM_CNT				0xb2
#define APM_CNT_SMMSTORE		0xed
#define SMMSTORE_CMD_VBOOT_TRY_OTHER	9

typedef uint64_t efi_status;

struct table_header {
	uint64_t signature;
	uint32_t revision, header_size, crc32, reserved;
};

/* EFI_BOOT_SERVICES fino a ExitBootServices() (UEFI 2.10, 4.4) */
struct boot_services {
	struct table_header hdr;
	void *raise_tpl, *restore_tpl, *allocate_pages, *free_pages;
	efi_status (*get_memory_map)(uint64_t *size, void *map, uint64_t *key,
				     uint64_t *descriptor_size, uint32_t *descriptor_version);
	void *allocate_pool, *free_pool, *create_event, *set_timer, *wait_for_event,
	     *signal_event, *close_event, *check_event, *install_protocol_interface,
	     *reinstall_protocol_interface, *uninstall_protocol_interface,
	     *handle_protocol, *reserved, *register_protocol_notify, *locate_handle,
	     *locate_device_path, *install_configuration_table, *load_image,
	     *start_image, *exit, *unload_image;
	efi_status (*exit_boot_services)(void *image, uint64_t key);
};

/* EFI_SYSTEM_TABLE fino a BootServices (UEFI 2.10, 4.3) */
struct system_table {
	struct table_header hdr;
	void *firmware_vendor;
	uint32_t firmware_revision;
	void *console_in_handle, *con_in, *console_out_handle, *con_out,
	     *standard_error_handle, *std_err, *runtime_services;
	struct boot_services *boot_services;
};

_Static_assert(__builtin_offsetof(struct boot_services, get_memory_map) == 0x38, "GetMemoryMap");
_Static_assert(__builtin_offsetof(struct boot_services, exit_boot_services) == 0xe8, "ExitBootServices");
_Static_assert(__builtin_offsetof(struct system_table, boot_services) == 0x60, "BootServices");

static uint8_t memory_map[64 * 1024];
static uint8_t smm_param[64];		/* un parametro qualsiasi, fuori dalla SMRAM */

static inline void outb(uint16_t port, uint8_t value)
{
	__asm__ volatile("outb %0, %1" : : "a"(value), "Nd"(port));
}

static inline uint8_t inb(uint16_t port)
{
	uint8_t value;

	__asm__ volatile("inb %1, %0" : "=a"(value) : "Nd"(port));
	return value;
}

/* La UART resta come l'ha programmata il firmware */
static void serial_putc(char c)
{
	int i;

	for (i = 0; i < 100000 && !(inb(COM1 + 5) & 0x20); i++)
		;
	outb(COM1, (uint8_t)c);
}

static void serial_puts(const char *s)
{
	for (; *s; s++) {
		if (*s == '\n')
			serial_putc('\r');
		serial_putc(*s);
	}
}

static void serial_putu(uint32_t value)
{
	char digits[11];
	int i = sizeof(digits) - 1;

	digits[i] = '\0';
	do {
		digits[--i] = (char)('0' + value % 10);
		value /= 10;
	} while (value);
	serial_puts(&digits[i]);
}

/* Come SmmStoreLib: comando in AH, APM_CNT_SMMSTORE in AL, parametro in EBX;
   l'SMI handler mette la risposta in EAX */
static uint32_t smmstore(uint8_t command, void *param)
{
	uint32_t eax = ((uint32_t)command << 8) | APM_CNT_SMMSTORE;

	__asm__ volatile("outb %%al, %1"
			 : "+a"(eax)
			 : "N"(APM_CNT), "b"((uint32_t)(uintptr_t)param)
			 : "memory");
	return eax;
}

static __attribute__((noreturn)) void stop(void)
{
	for (;;)
		__asm__ volatile("cli; hlt");
}

/* prima la risposta, poi la riga: con DEBUG_SMI anche coreboot scrive
   sulla seriale, durante l'SMI */
static void try_other(const char *when)
{
	uint32_t ret = smmstore(SMMSTORE_CMD_VBOOT_TRY_OTHER, smm_param);

	serial_puts("ouroBIOS slot lock test: TRY_OTHER ");
	serial_puts(when);
	serial_puts(" ExitBootServices: ");
	serial_putu(ret);
	serial_puts("\n");
}

efi_status efi_main(void *image, struct system_table *st)
{
	struct boot_services *bs = st->boot_services;
	uint64_t size, key, descriptor_size;
	uint32_t descriptor_version;
	int i;

	try_other("before");
	serial_puts("ouroBIOS slot lock test: ExitBootServices()\n");
	for (i = 0; i < 8; i++) {
		size = sizeof(memory_map);
		if (bs->get_memory_map(&size, memory_map, &key, &descriptor_size, &descriptor_version))
			continue;
		if (!bs->exit_boot_services(image, key))
			break;
	}
	if (i == 8) {
		serial_puts("ouroBIOS slot lock test: ExitBootServices() failed\n");
		stop();
	}

	try_other("after");
	stop();
}
