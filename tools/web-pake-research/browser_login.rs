// SPDX-License-Identifier: GPL-3.0-or-later
//! One browser login slot, for the pinned research profile. No transport.

#[path = "ocbox_profile/mod.rs"]
mod profile;

use opaque_ke::{
    rand::rand_core::{Infallible, TryCryptoRng, TryRng},
    ClientLogin, ClientLoginFinishParameters, CredentialResponse, Identifiers,
};
use profile::Suite;
use std::cell::RefCell;
use zeroize::{Zeroize, Zeroizing};

const IO_SIZE: usize = 1024;
const KE2: usize = 320;
const BINDING: usize = 32;
const FINISH_SIZE: usize = KE2 + 3 * BINDING;

#[link(wasm_import_module = "entropy")]
unsafe extern "C" {
    // Must fill the exact slice or trap. There is no entropy fallback.
    fn fill(ptr: *mut u8, len: usize);
}

struct BrowserEntropy;

impl TryRng for BrowserEntropy {
    type Error = Infallible;

    fn try_fill_bytes(&mut self, out: &mut [u8]) -> Result<(), Infallible> {
        unsafe { fill(out.as_mut_ptr(), out.len()) };
        Ok(())
    }

    fn try_next_u32(&mut self) -> Result<u32, Infallible> {
        let mut bytes = [0; 4];
        self.try_fill_bytes(&mut bytes)?;
        Ok(u32::from_le_bytes(bytes))
    }

    fn try_next_u64(&mut self) -> Result<u64, Infallible> {
        let mut bytes = [0; 8];
        self.try_fill_bytes(&mut bytes)?;
        Ok(u64::from_le_bytes(bytes))
    }
}

impl TryCryptoRng for BrowserEntropy {}

struct Pending {
    login: ClientLogin<Suite>,
    password: Zeroizing<[u8; IO_SIZE]>,
    size: usize,
}

struct Slot {
    io: [u8; IO_SIZE],
    pending: Option<Pending>,
    fresh: bool,
}

impl Slot {
    fn start(&mut self, size: usize) -> Option<usize> {
        let fresh = std::mem::replace(&mut self.fresh, false);
        self.pending = None;
        if !fresh || !(1..=IO_SIZE).contains(&size) {
            self.io.zeroize();
            return None;
        }
        let mut password = Zeroizing::new([0; IO_SIZE]);
        password[..size].copy_from_slice(&self.io[..size]);
        self.io.zeroize();
        let start = ClientLogin::<Suite>::start(&mut BrowserEntropy, &password[..size]).ok()?;
        self.pending = Some(Pending {
            login: start.state,
            password,
            size,
        });
        let message = start.message.serialize();
        self.io[..message.len()].copy_from_slice(message.as_slice());
        Some(message.len())
    }

    fn finish(&mut self) -> Option<usize> {
        // Consume before processing, even on a malformed or mismatched KE2.
        self.fresh = false;
        let pending = self.pending.take();
        let mut frame = [0; FINISH_SIZE];
        frame.copy_from_slice(&self.io[..FINISH_SIZE]);
        self.io.zeroize();
        let pending = pending?;
        let response = CredentialResponse::deserialize(&frame[..KE2]).ok()?;
        let mut verified = pending.login.finish(
            &mut BrowserEntropy,
            &pending.password[..pending.size],
            response,
            ClientLoginFinishParameters::new(
                Some(&frame[KE2..KE2 + BINDING]),
                Identifiers {
                    client: Some(&frame[KE2 + BINDING..KE2 + 2 * BINDING]),
                    server: Some(&frame[KE2 + 2 * BINDING..]),
                },
                None,
            ),
        ).ok()?;
        verified.session_key.zeroize();
        verified.export_key.zeroize();
        let message = verified.message.serialize();
        self.io[..message.len()].copy_from_slice(message.as_slice());
        Some(message.len())
    }
}

thread_local! {
    static SLOT: RefCell<Slot> = const { RefCell::new(Slot {
        io: [0; IO_SIZE], pending: None, fresh: true,
    }) };
}

#[unsafe(no_mangle)]
pub extern "C" fn io_addr() -> *mut u8 {
    SLOT.with(|slot| slot.borrow_mut().io.as_mut_ptr())
}

#[unsafe(no_mangle)]
pub extern "C" fn io_size() -> usize {
    IO_SIZE
}

#[unsafe(no_mangle)]
pub extern "C" fn login_start(size: usize) -> i32 {
    SLOT.with(|slot| slot.borrow_mut().start(size).map_or(-1, |size| size as i32))
}

#[unsafe(no_mangle)]
pub extern "C" fn login_finish() -> i32 {
    SLOT.with(|slot| slot.borrow_mut().finish().map_or(-1, |size| size as i32))
}

#[unsafe(no_mangle)]
pub extern "C" fn discard() -> i32 {
    SLOT.with(|slot| {
        let mut slot = slot.borrow_mut();
        slot.fresh = false;
        slot.pending = None;
        slot.io.zeroize();
        1
    })
}

fn main() {}
