// SPDX-License-Identifier: GPL-3.0-or-later
//! OCOR v1 research helper: one operation, no listener, enrollment or storage.

#[path = "ocbox_profile/mod.rs"]
mod profile;

use opaque_ke::{
    rand::{rand_core::UnwrapErr, rngs::SysRng},
    RegistrationRequest, RegistrationUpload, ServerRegistration, ServerSetup,
};
use profile::Suite;
use std::{
    io::{self, Read, Write},
    process::ExitCode,
};
use zeroize::Zeroizing;

const MAGIC: &[u8; 5] = b"OCOR\x01";
// SHA-256 of the opaque-ke research profile's ASCII label, not a credential ID.
const PROFILE: [u8; 32] = [
    0xf7, 0x15, 0x44, 0xa3, 0x82, 0xec, 0x1b, 0x3d, 0x12, 0x80, 0xe8, 0xa1, 0x3b, 0xa9, 0x41, 0x3d,
    0x70, 0x5e, 0x8d, 0xd6, 0x2b, 0x68, 0x72, 0x4c, 0xec, 0xd6, 0xe0, 0x08, 0xf7, 0x4f, 0x12, 0x2c,
];
const HEADER: usize = MAGIC.len() + 1 + PROFILE.len();
const SETUP: usize = 128;
const CREDENTIAL_ID: usize = 32;
const REQUEST: usize = 32;
const RESPONSE: usize = 64;
const UPLOAD: usize = 192;
const RESPOND_INPUT: usize = SETUP + CREDENTIAL_ID + REQUEST;

enum Operation {
    Setup,
    Respond,
    Validate,
}

impl Operation {
    fn parse(header: &[u8; HEADER]) -> Option<Self> {
        if &header[..MAGIC.len()] != MAGIC || header[MAGIC.len() + 1..] != PROFILE {
            return None;
        }
        match header[MAGIC.len()] {
            1 => Some(Self::Setup),
            2 => Some(Self::Respond),
            3 => Some(Self::Validate),
            _ => None,
        }
    }

    fn input_size(&self) -> usize {
        match self {
            Self::Setup => 0,
            Self::Respond => RESPOND_INPUT,
            Self::Validate => UPLOAD,
        }
    }

    fn run(&self, input: &[u8], output: &mut [u8; UPLOAD]) -> Option<usize> {
        match self {
            Self::Setup => {
                let setup = ServerSetup::<Suite>::new(&mut UnwrapErr(SysRng));
                let serialized = Zeroizing::new(setup.serialize());
                if serialized.len() != SETUP {
                    return None;
                }
                output[..SETUP].copy_from_slice(serialized.as_slice());
                Some(SETUP)
            }
            Self::Respond => {
                let setup = ServerSetup::<Suite>::deserialize(&input[..SETUP]).ok()?;
                let canonical = Zeroizing::new(setup.serialize());
                if canonical.as_slice() != &input[..SETUP] {
                    return None;
                }
                let credential_end = SETUP + CREDENTIAL_ID;
                let request = RegistrationRequest::deserialize(&input[credential_end..]).ok()?;
                if request.serialize().as_slice() != &input[credential_end..] {
                    return None;
                }
                let response =
                    ServerRegistration::start(&setup, request, &input[SETUP..credential_end])
                        .ok()?;
                let serialized = response.message.serialize();
                if serialized.len() != RESPONSE {
                    return None;
                }
                output[..RESPONSE].copy_from_slice(serialized.as_slice());
                Some(RESPONSE)
            }
            Self::Validate => {
                // This checks the pinned serialization only. The library cannot
                // authenticate the upload, bind it to a prior request or prove
                // that the account can log in.
                let upload = RegistrationUpload::<Suite>::deserialize(input).ok()?;
                let record = ServerRegistration::finish(upload);
                let serialized = Zeroizing::new(record.serialize());
                if serialized.len() != UPLOAD || serialized.as_slice() != input {
                    return None;
                }
                output.copy_from_slice(serialized.as_slice());
                Some(UPLOAD)
            }
        }
    }
}

fn require_eof(input: &mut impl Read) -> io::Result<()> {
    loop {
        match input.read(&mut [0]) {
            Ok(0) => return Ok(()),
            Ok(_) => return Err(io::ErrorKind::InvalidData.into()),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

fn exchange(input: &mut impl Read, output: &mut impl Write) -> io::Result<()> {
    let mut header = [0; HEADER];
    input.read_exact(&mut header)?;
    let operation = Operation::parse(&header).ok_or(io::ErrorKind::InvalidData)?;
    let mut wire = Zeroizing::new([0; UPLOAD]);
    let size = operation.input_size();
    input.read_exact(&mut wire[..size])?;
    require_eof(input)?;

    let mut reply = Zeroizing::new([0; UPLOAD]);
    let size = operation.run(&wire[..size], &mut reply);
    drop(wire);
    output.write_all(&header)?;
    output.write_all(&[u8::from(size.is_none())])?;
    if let Some(size) = size {
        output.write_all(&reply[..size])?;
    }
    output.flush()
}

fn main() -> ExitCode {
    // The pinned entropy API can panic on system entropy failure. Do not emit
    // diagnostics from this private pipe helper, including on panic.
    std::panic::set_hook(Box::new(|_| {}));
    if std::env::args_os().nth(1).is_some() {
        return ExitCode::FAILURE;
    }
    match exchange(&mut io::stdin().lock(), &mut io::stdout().lock()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => ExitCode::FAILURE,
    }
}
